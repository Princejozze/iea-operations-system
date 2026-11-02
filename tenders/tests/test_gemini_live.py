"""
OPT-IN: one REAL, BILLED Gemini call on the GASCO tender, to measure what extraction costs.

Skipped unless all of these are set in the environment:

    RUN_GEMINI_TESTS=1
    GEMINI_API_KEY=...      your key
    GEMINI_MODEL=...        the model to cost (never hardcoded in the code base)

Optional, to have the cost computed (USD per million tokens, from the model's published price list;
thinking tokens are billed as output):

    GEMINI_PRICE_INPUT_USD_PER_MTOK=...
    GEMINI_PRICE_OUTPUT_USD_PER_MTOK=...

Run only this test:   python manage.py test tenders.tests.test_gemini_live

It prints the report to stderr and writes it to <tempdir>/iea_gemini_gasco_report.json. The ordinary
suite never makes an API call: this module is skipped there, and every other test uses a fake.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.core.files.base import ContentFile
from django.test import TestCase

from tenders.models import ExtractionOutcome as X, Tender, TenderStatus as S
from tenders.pipeline import service
from tenders.pipeline.extractor import GeminiExtractor, build_request

from .helpers import TenderFixtures
from .pipeline_helpers import GASCO, CachedConverter, fixture_bytes, requires_fixtures

ENABLED = os.environ.get("RUN_GEMINI_TESTS") == "1"
REPORT = Path(tempfile.gettempdir()) / "iea_gemini_gasco_report.json"


def price(name):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


@skipUnless(ENABLED, "opt-in: set RUN_GEMINI_TESTS=1 (makes one real, billed API call)")
@requires_fixtures(GASCO)
class OneRealCallOnGasco(TenderFixtures, TestCase):
    def test_extraction_cost_and_correctness(self):
        if not (os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_MODEL")):
            self.skipTest("set GEMINI_API_KEY and GEMINI_MODEL")
        tender = Tender.objects.create_from_upload(
            uploaded_by=self.pm, pdf=ContentFile(fixture_bytes(GASCO), name=GASCO))
        service.prepare_extraction(tender, converter=CachedConverter())
        extractor = GeminiExtractor.from_environment()
        request = build_request(service.ready_sections(tender), tender.extraction.plan)

        service.extract_fields(tender, extractor)  # the one billed call
        tender.refresh_from_db()
        ext = tender.extraction
        ext.refresh_from_db()

        usage = ext.usage
        billed_out = usage.get("output_tokens", 0) + usage.get("thoughts_tokens", 0)
        p_in, p_out = price("GEMINI_PRICE_INPUT_USD_PER_MTOK"), price("GEMINI_PRICE_OUTPUT_USD_PER_MTOK")
        cost = None if p_in is None or p_out is None else (
            usage.get("prompt_tokens", 0) * p_in + billed_out * p_out) / 1_000_000
        report = {
            "model": ext.model_name, "outcome": ext.outcome, "usage": usage,
            "estimated_input_tokens_before_the_call": request.est_input_tokens,
            "prompt_chars": len(request.prompt()), "parts": [p.label for p in request.parts],
            "billed_output_tokens_incl_thinking": billed_out,
            "price_usd_per_mtok": {"input": p_in, "output": p_out},
            "cost_usd_for_this_tender": cost,
            "fields": {k: {"value": v["value"], "confidence": v["confidence"], "page": v["source_page"], "flags": v["flags"]}
                       for k, v in ext.fields.items()},
        }
        REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        sys.stderr.write("\n--- GASCO real extraction ---\n" + json.dumps(report, indent=2, default=str) + f"\n(report: {REPORT})\n")

        # R12 and §5 are checked on the real answer, whatever model is configured.
        self.assertIn(ext.outcome, (X.EXTRACTED, X.PARTIAL), ext.message)
        self.assertEqual(tender.status, S.PENDING_VERIFICATION)
        f = ext.fields
        self.assertEqual(f["official_reference"]["value"], "TR24/001/2026/2027/G/133")
        self.assertTrue((f["submission_deadline"]["value"] or "").startswith("2026-11-13"), f["submission_deadline"])
        self.assertEqual(f["submission_deadline"]["source_page"], 77)  # Tender Data Sheet item 36
        for name in ("clarification_deadline", "site_visit_required", "site_visit_date"):
            self.assertIsNone(f[name]["value"], f"{name} must be null: GASCO states none (§5)")
        self.assertEqual((tender.title, tender.official_reference), ("", ""))  # nothing reached Tender (R12)
        self.assertGreater(usage.get("prompt_tokens", 0), 0)
