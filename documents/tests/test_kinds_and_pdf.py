"""MAF, board resolution, PDF output, the single letterhead, and registry entries
for every kind. Runs on PostgreSQL."""

from pathlib import Path

import pymupdf
from django.core.exceptions import ValidationError
from django.template import engines
from django.test import SimpleTestCase, TestCase, override_settings

import documents
from documents.models import Document, DocumentKind
from documents.rendering import KINDS, render, render_pdf
from documents.services import build_email, create_document

from .test_rendering import rfq_context


def maf_context(**overrides):
    context = {
        "manufacturer_name": "Tuttnauer Ltd",
        "manufacturer_address": "Jerusalem, Israel",
        "tender_reference": "PA/001/2026",
        "procuring_entity": "Muhimbili National Hospital",
        "tender_title": "Supply of laboratory equipment",
        "goods": [{"description": "Autoclave 100 L", "model": "3870EA"}],
    }
    context.update(overrides)
    return context


def resolution_context(**overrides):
    context = {
        "tender_reference": "PA/001/2026",
        "procuring_entity": "Muhimbili National Hospital",
        "signatory_name": "Gift Minja",
        "signatory_title": "Director",
        "directors": [{"name": "A Director", "title": "Chairman"}, {"name": "B Director"}],
    }
    context.update(overrides)
    return context


CONTEXTS = {"RFQ": rfq_context, "MAF": maf_context, "BOARD_RESOLUTION": resolution_context}


def pdf_text(data):
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return " ".join(page.get_text() for page in doc)


class EveryKindHasATemplateTests(SimpleTestCase):
    def test_every_document_kind_is_renderable_and_has_a_test_context(self):
        self.assertEqual(set(KINDS), set(DocumentKind.values))
        self.assertEqual(set(CONTEXTS), set(DocumentKind.values))


class MafTests(SimpleTestCase):
    def test_content(self):
        out = render("MAF", maf_context())
        self.assertEqual(out.subject, "Manufacturer's Authorisation Form - PA/001/2026")
        for needle in (
            "Tuttnauer Ltd", "Jerusalem", "Muhimbili National Hospital", "PA/001/2026",
            "Autoclave 100 L", "3870EA", "Imperial East Africa Limited", "Signature:",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, out.html)

    def test_bidder_defaults_to_letterhead_company_and_can_be_overridden(self):
        self.assertIn("authorise <strong>Imperial East Africa Limited", render("MAF", maf_context()).html)
        self.assertIn("<strong>Other Co</strong>", render("MAF", maf_context(bidder_name="Other Co")).html)

    def test_unknown_signatory_leaves_a_blank_line_to_fill_in(self):
        self.assertIn("Name: ____", render("MAF", maf_context()).html)
        self.assertIn("Name: Jo Mfr", render("MAF", maf_context(signatory_name="Jo Mfr")).html)

    def test_user_data_is_escaped(self):
        html = render("MAF", maf_context(manufacturer_name="<script>x</script>")).html
        self.assertNotIn("<script>", html)

    def test_missing_fields_are_all_reported(self):
        with self.assertRaises(ValidationError) as caught:
            render("MAF", {"goods": [{"model": "x"}]})
        text = " ".join(caught.exception.messages)
        for needle in ("manufacturer_name", "tender_reference", "procuring_entity",
                       "Goods row 1: missing 'description'"):
            self.assertIn(needle, text)

    def test_empty_goods_refused(self):
        with self.assertRaises(ValidationError):
            render("MAF", maf_context(goods=[]))


class ResolutionTests(SimpleTestCase):
    def test_content(self):
        out = render("BOARD_RESOLUTION", resolution_context())
        self.assertEqual(out.subject, "Board Resolution - PA/001/2026")
        for needle in ("Muhimbili National Hospital", "PA/001/2026", "Gift Minja", "Director",
                       "A Director, Chairman", "B Director", "IT IS RESOLVED"):
            with self.subTest(needle=needle):
                self.assertIn(needle, out.html)

    def test_one_signature_line_per_director(self):
        self.assertEqual(render("BOARD_RESOLUTION", resolution_context()).html.count("Signature:"), 2)

    def test_missing_directors_or_signatory_refused(self):
        for key in ("directors", "signatory_name", "signatory_title", "procuring_entity"):
            ctx = resolution_context()
            del ctx[key]
            with self.subTest(key=key), self.assertRaises(ValidationError):
                render("BOARD_RESOLUTION", ctx)


class PdfTests(SimpleTestCase):
    def test_every_kind_renders_a_real_pdf_with_its_content(self):
        for kind, make in CONTEXTS.items():
            with self.subTest(kind=kind):
                data = render_pdf(kind, make())
                self.assertTrue(data.startswith(b"%PDF"))
                text = pdf_text(data)
                self.assertIn("Imperial East Africa Limited", text)
                self.assertIn("PLACEHOLDER LETTERHEAD", text)

    def test_long_documents_paginate(self):
        items = [
            {"reference": f"R-{n}", "specification": "Spec line\n" * 6, "quantity": n + 1}
            for n in range(80)
        ]
        data = render_pdf("RFQ", rfq_context(items=items))
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            self.assertGreater(doc.page_count, 1)
        self.assertIn("R-79", pdf_text(data))


class LetterheadInOnePlaceTests(SimpleTestCase):
    TEMPLATES = Path(documents.__file__).parent / "templates" / "documents"

    def test_placeholder_marker_lives_in_exactly_one_template(self):
        holders = [
            p.name
            for p in self.TEMPLATES.glob("*.html")
            if "PLACEHOLDER LETTERHEAD" in p.read_text(encoding="utf-8")
        ]
        self.assertEqual(holders, ["_letterhead.html"])

    def test_no_kind_template_prints_its_own_header(self):
        for p in self.TEMPLATES.glob("*.html"):
            if p.name in ("_letterhead.html", "letterhead.html"):
                continue
            source = p.read_text(encoding="utf-8").replace("default:letterhead.company_name", "")
            with self.subTest(template=p.name):
                self.assertNotIn("<h1", source)
                self.assertNotIn("letterhead.address", source)

    def test_swapping_the_partial_rebrands_every_kind(self):
        swapped = [{
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "OPTIONS": {
                "loaders": [
                    ("django.template.loaders.locmem.Loader",
                     {"documents/_letterhead.html": "<p>REAL BRAND</p>"}),
                    "django.template.loaders.app_directories.Loader",
                ]
            },
        }]
        with override_settings(TEMPLATES=swapped):
            engines._templates = None
            try:
                for kind, make in CONTEXTS.items():
                    with self.subTest(kind=kind):
                        html = render(kind, make()).html
                        self.assertIn("REAL BRAND", html)
                        self.assertNotIn("PLACEHOLDER LETTERHEAD", html)
            finally:
                engines._templates = None


class RegistryForEveryKindTests(TestCase):
    def test_each_kind_creates_a_registry_entry_with_a_stored_pdf(self):
        for kind, make in CONTEXTS.items():
            with self.subTest(kind=kind):
                before = Document.objects.count()
                doc = create_document(kind, make(), to=["x@example.com"])
                self.assertEqual(Document.objects.count(), before + 1)
                doc.refresh_from_db()
                self.assertEqual(doc.kind, kind)
                self.assertTrue(doc.pdf.name.endswith(".pdf"))
                with doc.pdf.open("rb") as handle:
                    self.assertIn("Imperial East Africa Limited", pdf_text(handle.read()))

    def test_r8_cc_applies_to_rfq_only(self):
        self.assertEqual(
            create_document("RFQ", rfq_context(), cc=[]).cc, ["sales@imperialeastafrica.tz"]
        )
        self.assertEqual(create_document("MAF", maf_context(), cc=[]).cc, [])
        self.assertEqual(create_document("BOARD_RESOLUTION", resolution_context()).cc, [])

    def test_rejected_document_writes_no_registry_row(self):
        with self.assertRaises(ValidationError):
            create_document("MAF", maf_context(), to=["not-an-address"])
        self.assertEqual(Document.objects.count(), 0)

    def test_email_attaches_the_stored_pdf(self):
        message = build_email(create_document("RFQ", rfq_context(), to=["s@example.com"]))
        name, content, mimetype = message.attachments[0]
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(name.endswith(".pdf"))
        self.assertIn("IEA-2026-0042", pdf_text(content))

    def test_long_reference_still_fits_the_file_name(self):
        doc = create_document(
            "BOARD_RESOLUTION", resolution_context(tender_reference="PA/" + "9" * 120)
        )
        self.assertTrue(doc.pdf.name)
