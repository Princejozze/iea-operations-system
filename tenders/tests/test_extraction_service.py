"""
Asking the model and storing the answer (CLAUDE.md §5 step 5-6, §6 "Extraction", R12), against
PostgreSQL, with a FAKE extractor: this suite never makes an API call.
"""

import datetime
import json

from django.test import TestCase

from core.models import AuditAction, AuditEntry, Client
from tenders.models import ExtractionOutcome as X, Tender, TenderExtraction, TenderStatus as S, TransitionError
from tenders.pipeline import service
from tenders.pipeline.converter import PyMuPDF4LLMConverter
from tenders.pipeline.extractor import (
    ExtractionConfigError, ExtractionError, ExtractionUnavailable, build_request, forms_from_plan,
)
from tenders.pipeline.sections import Group as G

from .helpers import TenderFixtures, backdate_updated_at, confirmed_fields, force_status, status_entries
from .pipeline_helpers import (
    GASCO, CachedConverter, FakeExtractor, StaticConverter, field, fixture_bytes, good_answer, listing,
    mini_tender_pages, requires_fixtures, scanned_pdf,
)

PDF = b"%PDF-1.4 placeholder bytes: the StaticConverter never reads them"


class ExtractionCase(TenderFixtures, TestCase):
    def upload(self, data=PDF, name="tender.pdf"):
        from django.core.files.base import ContentFile

        return Tender.objects.create_from_upload(uploaded_by=self.pm, pdf=ContentFile(data, name=name))

    def prepared(self, pages=None):
        tender = self.upload()
        service.prepare_extraction(tender, converter=StaticConverter(pages or mini_tender_pages()))
        tender.refresh_from_db()
        return tender

    def extract(self, answer=None, **kw):
        tender = self.prepared(kw.pop("pages", None))
        fake = FakeExtractor(good_answer() if answer is None else answer, **kw)
        service.extract_fields(tender, fake)
        tender.refresh_from_db()
        tender.extraction.refresh_from_db()
        return tender, fake


class SuccessTests(ExtractionCase):
    def test_a_good_answer_is_stored_and_the_tender_moves_to_pending_verification(self):
        tender, fake = self.extract()
        ext = tender.extraction
        self.assertEqual((ext.outcome, tender.status), (X.EXTRACTED, S.PENDING_VERIFICATION))
        self.assertEqual(ext.message, "")
        self.assertEqual(fake.calls, 1)

    def test_every_value_has_a_confidence_and_a_source_page(self):
        tender, _ = self.extract()
        f = tender.extraction.fields
        for name in ("official_reference", "title", "client_name", "submission_deadline"):
            with self.subTest(field=name):
                self.assertIsNotNone(f[name]["confidence"])
                self.assertIsNotNone(f[name]["source_page"])
        self.assertEqual((f["official_reference"]["value"], f["official_reference"]["source_page"]), ("TR9/2026/2027/G/1", 4))
        self.assertEqual((f["submission_deadline"]["value"], f["submission_deadline"]["source_page"]),
                         ("2026-10-23T14:00:00", 4))
        self.assertEqual(f["special_requirements"]["value"][0]["source_page"], 6)

    def test_the_submission_deadline_is_extracted(self):
        tender, _ = self.extract()
        self.assertEqual(tender.extraction.fields["submission_deadline"]["value"], "2026-10-23T14:00:00")

    def test_clarification_deadline_and_site_visit_are_null_when_not_stated(self):
        # §5: usually not stated; the document's only mention is generic ("may request clarification").
        tender, _ = self.extract()
        f = tender.extraction.fields
        for name in ("clarification_deadline", "site_visit_required", "site_visit_date"):
            self.assertIsNone(f[name]["value"], name)
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)  # and that is not a defect

    def test_nothing_reaches_tender_r12(self):
        before = Tender.objects.values().get(pk=self.prepared().pk)
        tender, _ = self.extract()
        after = Tender.objects.values().get(pk=tender.pk)
        for name in Tender.CONFIRMED_FIELDS:
            attname = Tender._meta.get_field(name).attname
            self.assertEqual(after[attname], Tender.objects.values().get(pk=tender.pk)[attname])
        self.assertEqual((tender.title, tender.official_reference, tender.client_address), ("", "", ""))
        self.assertIsNone(tender.submission_deadline)
        self.assertEqual((tender.special_requirements, tender.required_forms, tender.bid_security_type), ("", "", ""))
        self.assertIsNone(before["submission_deadline"])

    def test_the_only_change_to_tender_is_its_status(self):
        tender, _ = self.extract()
        entries = AuditEntry.objects.filter(model="tenders.Tender", object_id=str(tender.pk)).exclude(action=AuditAction.CREATE)
        self.assertEqual([set(e.changes) for e in entries], [{"status"}])
        self.assertEqual(status_entries(tender).get().actor, None)  # the system, not a user

    def test_usage_model_and_raw_output_are_recorded(self):
        tender, fake = self.extract(usage={"prompt_tokens": 4321, "output_tokens": 210, "thoughts_tokens": 5,
                                           "cached_tokens": 0, "total_tokens": 4536})
        ext = tender.extraction
        self.assertEqual(ext.usage["prompt_tokens"], 4321)
        self.assertEqual(ext.model_name, "fake-model-1")
        self.assertEqual(json.loads(ext.raw_output), good_answer())
        self.assertIsNotNone(ext.extracted_at)

    def test_the_raw_output_stays_out_of_the_audit_trail(self):
        tender, _ = self.extract()
        for e in AuditEntry.objects.filter(model="tenders.TenderExtraction", object_id=str(tender.extraction.pk)):
            self.assertNotIn("TR9/2026/2027/G/1", json.dumps(e.changes.get("raw_output", "")))
            self.assertLess(len(json.dumps(e.changes)), 20000)

    def test_the_pm_carries_on_to_verification(self):
        tender, _ = self.extract()
        tender.confirm_extraction(self.pm, **confirmed_fields())
        self.assertEqual(tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_an_existing_client_is_suggested_never_assigned(self):
        client = Client.objects.create(name="Ministry of Health", short_name="MOH")
        tender, _ = self.extract()
        match = tender.extraction.fields["client_match"]
        self.assertEqual(match["value"], {"id": client.pk, "name": "Ministry of Health"})
        self.assertEqual(match["source"], "derived")
        self.assertIsNone(tender.client)

    def test_an_unknown_client_gets_no_suggestion(self):
        tender, _ = self.extract()
        self.assertNotIn("client_match", tender.extraction.fields)


class TheGuardRunsOnEveryAnswer(ExtractionCase):
    """The fake can be as wrong as a real model. What is stored is what the document supports."""

    def test_an_inferred_clarification_deadline_and_site_visit_are_dropped(self):
        answer = good_answer()
        answer["clarification_deadline"] = field(
            "2026-10-16T00:00:00", 4, "Tenderers may request clarification of the quotation documents before the submission deadline")
        answer["site_visit_required"] = field(True, 4, "Tenderers may request clarification of the quotation documents")
        tender, _ = self.extract(answer)
        f = tender.extraction.fields
        self.assertIsNone(f["clarification_deadline"]["value"])
        self.assertIn("date_not_in_quote", f["clarification_deadline"]["flags"])
        self.assertIsNone(f["site_visit_required"]["value"])
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)

    def test_a_hallucinated_deadline_is_dropped_and_the_extraction_is_partial(self):
        answer = good_answer()
        answer["submission_deadline"] = field("2026-12-25T10:00:00", 4, "submission deadline will be on 10:00 AM at 25/12/2026")
        tender, _ = self.extract(answer)
        self.assertIsNone(tender.extraction.fields["submission_deadline"]["value"])
        self.assertEqual((tender.extraction.outcome, tender.status), (X.PARTIAL, S.PENDING_VERIFICATION))
        self.assertIn("Some details could not be read", tender.extraction.message)

    def test_a_date_that_contradicts_its_own_quote_is_dropped(self):
        answer = good_answer()
        answer["submission_deadline"]["value"] = "2026-10-30T14:00:00"
        tender, _ = self.extract(answer)
        self.assertIsNone(tender.extraction.fields["submission_deadline"]["value"])

    def test_low_confidence_values_are_kept_and_flagged_not_blocked(self):
        answer = good_answer()
        answer["title"]["confidence"] = 0.2
        tender, _ = self.extract(answer)
        rec = tender.extraction.fields["title"]
        self.assertEqual(rec["value"], "Procurement of Laboratory Equipment")
        self.assertTrue(rec["low_confidence"])
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)


class PartialAndFailure(ExtractionCase):
    def test_a_partial_answer_still_moves_the_tender_on(self):
        answer = good_answer()
        answer["title"] = field(None, None, None, None)
        answer["client_name"] = field(None, None, None, None)
        tender, _ = self.extract(answer)
        self.assertEqual((tender.extraction.outcome, tender.status), (X.PARTIAL, S.PENDING_VERIFICATION))
        self.assertIsNotNone(tender.extraction.fields["submission_deadline"]["value"])  # what was read is kept

    def test_an_empty_answer_is_a_failure_with_empty_fields_and_the_tender_moves_on(self):
        tender, _ = self.extract({})
        ext = tender.extraction
        self.assertEqual((ext.outcome, tender.status), (X.FAILED, S.PENDING_VERIFICATION))
        self.assertIn("Nothing could be read", ext.message)
        self.assertTrue(all(r["value"] is None for r in ext.fields.values()))

    def test_an_answer_of_all_nulls_is_a_failure(self):
        tender, _ = self.extract({n: field(None, None, None, None) for n in good_answer()})
        self.assertEqual(tender.extraction.outcome, X.FAILED)

    def test_an_extraction_error_leaves_empty_fields_and_never_traps_the_tender(self):
        tender = self.prepared()
        with self.assertLogs("tenders.pipeline.service", level="ERROR"):
            service.extract_fields(tender, FakeExtractor(error=ExtractionError("API said no")))
        tender.refresh_from_db()
        self.assertEqual((tender.extraction.outcome, tender.status), (X.FAILED, S.PENDING_VERIFICATION))
        self.assertEqual(tender.extraction.fields, {})
        self.assertIn("Automatic extraction failed", tender.extraction.message)
        self.assertNotIn("API said no", tender.extraction.message)  # internals do not reach the PM

    def test_a_briefly_unavailable_service_leaves_the_tender_for_the_next_run(self):
        tender = self.prepared()
        busy = FakeExtractor(error=ExtractionUnavailable("The Gemini request failed (ServerError 503)."))
        with self.assertLogs("tenders.pipeline.service", level="WARNING"):
            ext = service.extract_fields(tender, busy)
        tender.refresh_from_db()
        self.assertEqual((ext.outcome, tender.status), (X.PREPARED, S.EXTRACTING))  # not FAILED, not moved on
        ext = service.extract_fields(tender, FakeExtractor(good_answer()))  # the next run asks again
        tender.refresh_from_db()
        self.assertEqual((ext.outcome, tender.status), (X.EXTRACTED, S.PENDING_VERIFICATION))

    def test_any_unexpected_exception_does_the_same(self):
        for exc in (RuntimeError("boom"), ValueError("bad"), KeyError("x"), TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                tender = self.prepared()
                with self.assertLogs("tenders.pipeline.service", level="ERROR"):
                    service.extract_fields(tender, FakeExtractor(error=exc))
                tender.refresh_from_db()
                self.assertEqual((tender.extraction.outcome, tender.status), (X.FAILED, S.PENDING_VERIFICATION))

    def test_a_failure_still_lets_the_pm_type_the_tender_in(self):
        tender = self.prepared()
        with self.assertLogs("tenders.pipeline.service", level="ERROR"):
            service.extract_fields(tender, FakeExtractor(error=RuntimeError("boom")))
        tender.refresh_from_db()
        tender.confirm_extraction(self.pm, **confirmed_fields())
        self.assertEqual(tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_a_broken_answer_shape_is_a_failure_not_a_crash(self):
        class Weird(FakeExtractor):
            def extract(self, request):
                raw = super().extract(request)
                raw.fields = {"title": object()}  # not even JSON-able
                return raw

        tender = self.prepared()
        service.extract_fields(tender, Weird(good_answer()))
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.PENDING_VERIFICATION)

    def test_not_configured_is_an_operations_problem_so_the_claim_is_released(self):
        tender = self.prepared()
        with self.assertRaises(ExtractionConfigError):
            service.extract_fields(tender, FakeExtractor(error=ExtractionConfigError("GEMINI_MODEL is not set")))
        tender.refresh_from_db()
        self.assertEqual((tender.status, tender.extraction.outcome), (S.EXTRACTING, X.PREPARED))
        # fixed: the next run picks it up
        service.extract_fields(tender, FakeExtractor(good_answer()))
        tender.refresh_from_db()
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)


class WhatIsSent(ExtractionCase):
    def test_only_the_metadata_and_eligibility_sections_are_sent_for_the_mini_tender(self):
        tender, fake = self.extract()
        request = fake.requests[0]
        labels = [p.label for p in request.parts]
        self.assertEqual(len(labels), 3)  # metadata, eligibility, and forms (this TOC lists no forms)
        prompt = request.prompt()
        self.assertIn("<!-- page 4 -->", prompt)
        self.assertNotIn("GFS Code", prompt)  # the goods schedule is parsed locally, never sent
        self.assertNotIn("Technical Requirements", prompt)  # nor the specifications

    def test_the_forms_section_is_sent_only_when_the_toc_does_not_name_the_forms(self):
        tender, fake = self.extract()
        self.assertEqual(forms_from_plan(tender.extraction.plan), [])
        self.assertIn("required_forms", fake.requests[0].field_names)

    def test_a_section_that_could_not_be_located_is_replaced_by_the_whole_document(self):
        pages = mini_tender_pages()
        pages[6] = "Nothing recognisable here. " * 20  # the criteria pages lose their signature
        tender, fake = self.extract(pages=pages)
        request = fake.requests[0]
        self.assertEqual([p.label for p in request.parts], ["WHOLE DOCUMENT"])
        self.assertEqual(request.parts[0].ranges, ((1, 12),))
        self.assertIn("SCHEDULE OF REQUIREMENTS .... p.7", request.toc_hint)  # the TOC travels as a hint
        self.assertEqual(request.prompt().count("<!-- page "), 12)
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)  # and extraction still works

    def test_the_guard_checks_quotes_against_exactly_what_was_sent(self):
        # page 8 (the goods schedule) is NOT sent in the narrow request, so a quote taken from it
        # cannot ground anything.
        answer = good_answer()
        answer["title"] = field("Supply of Laboratory Equipment", 8, "Supply of Laboratory Equipment")
        tender, _ = self.extract(answer)
        rec = tender.extraction.fields["title"]
        self.assertIn("quote_not_found", rec["flags"])
        self.assertLessEqual(rec["confidence"], 0.4)


@requires_fixtures(GASCO)
class RealTenderRequest(ExtractionCase):
    converter = CachedConverter()

    def test_gasco_sends_two_narrow_sections_and_takes_the_forms_from_the_toc(self):
        tender = self.upload(fixture_bytes(GASCO), name=GASCO)
        service.prepare_extraction(tender, converter=self.converter)
        fake = FakeExtractor({})
        service.extract_fields(tender, fake)
        request = fake.requests[0]
        self.assertEqual(len(request.parts), 2)
        self.assertNotIn("required_forms", request.field_names)
        prompt = request.prompt()
        self.assertIn("Deadline for Submission of Tenders", prompt.replace("<br>", " "))
        self.assertIn("Authorization is required", prompt)  # page 73, as raw text
        self.assertNotIn("GFS Code", prompt)
        self.assertLess(request.est_input_tokens, 12000)  # a fraction of the ~57k-token document

    def test_gasco_forms_are_the_tocs_names_with_their_pages(self):
        tender = self.upload(fixture_bytes(GASCO), name=GASCO)
        service.prepare_extraction(tender, converter=self.converter)
        service.extract_fields(tender, FakeExtractor({}))
        tender.extraction.refresh_from_db()
        forms = tender.extraction.fields["required_forms"]
        self.assertEqual(forms["source"], "toc")
        self.assertIn(("Declaration Against Fraud and Corruption", 100), [(i["text"], i["source_page"]) for i in forms["value"]])


class ClaimsAndConcurrency(ExtractionCase):
    def test_a_second_call_does_not_ask_the_model_again(self):
        tender = self.prepared()
        fake = FakeExtractor(good_answer())
        first = service.extract_fields(tender, fake)
        again = service.extract_fields(tender, fake) if tender.status == S.EXTRACTING else None
        self.assertIsNone(again)  # the tender has moved on: nothing to do
        self.assertEqual((first.outcome, fake.calls), (X.EXTRACTED, 1))
        tender.refresh_from_db()
        with self.assertRaises(TransitionError):
            service.extract_fields(tender, fake)
        self.assertEqual(fake.calls, 1)

    def test_an_overlapping_run_cannot_make_the_same_model_call(self):
        tender = self.prepared()
        other = FakeExtractor(good_answer())
        seen = {}

        def while_the_first_run_is_calling_the_model(request):
            seen["inner"] = service.extract_fields(Tender.objects.get(pk=tender.pk), other)

        first = FakeExtractor(good_answer(), on_call=while_the_first_run_is_calling_the_model)
        service.extract_fields(tender, first)
        self.assertEqual((first.calls, other.calls), (1, 0))  # the second run found the claim and stood aside
        self.assertEqual(seen["inner"].outcome, X.READING)
        tender.refresh_from_db()
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)

    def test_a_fresh_reading_claim_is_respected_and_a_stale_one_is_taken_over(self):
        tender = self.prepared()
        ext = tender.extraction
        ext.outcome = X.READING
        ext.save()
        fake = FakeExtractor(good_answer())
        self.assertEqual(service.extract_fields(tender, fake).outcome, X.READING)
        self.assertEqual(fake.calls, 0)
        # Backdate the claim: Windows clock ticks are ~15.6ms, so a zero threshold is flaky.
        backdate_updated_at(ext)
        service.extract_fields(tender, fake)
        self.assertEqual(fake.calls, 1)
        tender.refresh_from_db()
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)

    def test_a_tender_still_converting_is_not_read(self):
        tender = self.upload()
        TenderExtraction.objects.create(tender=tender)  # PROCESSING
        fake = FakeExtractor(good_answer())
        self.assertEqual(service.extract_fields(tender, fake).outcome, X.PROCESSING)
        self.assertEqual(fake.calls, 0)

    def test_only_a_tender_in_extracting_is_read(self):
        tender = self.prepared()
        force_status(tender, S.PENDING_VERIFICATION)
        with self.assertRaises(TransitionError):
            service.extract_fields(tender, FakeExtractor(good_answer()))

    def test_a_tender_that_was_not_prepared_cannot_be_read(self):
        tender = self.upload()
        with self.assertRaises(TransitionError):
            service.extract_fields(tender, FakeExtractor(good_answer()))

    def test_a_tender_abandoned_while_the_model_was_answering_is_not_overwritten(self):
        tender = self.prepared()
        fake = FakeExtractor(good_answer(), on_call=lambda r: service.abandon_extraction(Tender.objects.get(pk=tender.pk)))
        service.extract_fields(tender, fake)
        tender.refresh_from_db()
        self.assertEqual((tender.extraction.outcome, tender.status), (X.FAILED, S.PENDING_VERIFICATION))
        self.assertEqual(tender.extraction.fields, {})  # the PM may already be typing: a late answer must not land
        self.assertEqual(status_entries(tender).count(), 1)

    def test_a_tender_withdrawn_while_the_model_was_answering_stays_withdrawn(self):
        tender = self.prepared()
        fake = FakeExtractor(good_answer(), on_call=lambda r: Tender.objects.get(pk=tender.pk).withdraw(self.md))
        service.extract_fields(tender, fake)
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.WITHDRAWN)
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)


class ProcessTender(ExtractionCase):
    def test_prepare_and_extract_in_one_call(self):
        tender = self.upload()
        fake = FakeExtractor(good_answer())
        result = service.process_tender(tender, converter=StaticConverter(mini_tender_pages()), extractor=fake)
        tender.refresh_from_db()
        self.assertEqual((result.outcome, tender.status, fake.calls), (X.EXTRACTED, S.PENDING_VERIFICATION, 1))

    def test_a_scan_never_reaches_the_model(self):
        tender = self.upload(scanned_pdf(3))
        fake = FakeExtractor(good_answer())
        result = service.process_tender(tender, converter=PyMuPDF4LLMConverter(), extractor=fake)
        tender.refresh_from_db()
        self.assertEqual((result.outcome, tender.status, fake.calls), (X.MANUAL_ENTRY, S.PENDING_VERIFICATION, 0))

    def test_without_an_extractor_it_only_prepares(self):
        tender = self.upload()
        result = service.process_tender(tender, converter=StaticConverter(mini_tender_pages()))
        tender.refresh_from_db()
        self.assertEqual((result.outcome, tender.status), (X.PREPARED, S.EXTRACTING))

    def test_it_is_safe_to_call_twice(self):
        tender = self.upload()
        fake, conv = FakeExtractor(good_answer()), StaticConverter(mini_tender_pages())
        service.process_tender(tender, converter=conv, extractor=fake)
        tender.refresh_from_db()
        second = service.process_tender(tender, converter=conv, extractor=fake) if tender.status == S.EXTRACTING else None
        self.assertIsNone(second)
        self.assertEqual((conv.calls, fake.calls), (1, 1))


class NeverTrapped(ExtractionCase):
    """§5 step 6: a tender is never left in EXTRACTING."""

    def later(self, minutes=31):
        return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)

    def test_a_tender_stuck_past_the_deadline_is_finished_with_empty_fields(self):
        tender = self.upload()  # never even prepared
        moved = service.fail_stuck(now=self.later())
        self.assertEqual([t.pk for t in moved], [tender.pk])
        tender.refresh_from_db()
        self.assertEqual((tender.status, tender.extraction.outcome), (S.PENDING_VERIFICATION, X.FAILED))
        self.assertEqual(tender.extraction.fields, {})
        self.assertIn("did not finish in time", tender.extraction.message)

    def test_every_stuck_stage_is_caught(self):
        a = self.upload()                                        # no extraction row yet
        b = self.prepared(); b.extraction.outcome = X.READING; b.extraction.save()  # model call in flight
        c = self.prepared()                                      # prepared, never read
        d = self.upload(); TenderExtraction.objects.create(tender=d)  # converting
        moved = service.fail_stuck(now=self.later())
        self.assertEqual({t.pk for t in moved}, {a.pk, b.pk, c.pk, d.pk})
        for t in (a, b, c, d):
            t.refresh_from_db()
            self.assertEqual(t.status, S.PENDING_VERIFICATION)

    def test_a_fresh_tender_is_left_alone(self):
        tender = self.upload()
        self.assertEqual(service.fail_stuck(), [])
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.EXTRACTING)

    def test_a_finished_tender_is_not_touched_again(self):
        tender, _ = self.extract()
        self.assertEqual(service.fail_stuck(now=self.later()), [])
        tender.extraction.refresh_from_db()
        self.assertEqual(tender.extraction.outcome, X.EXTRACTED)

    def test_the_sweep_is_idempotent(self):
        tender = self.upload()
        self.assertEqual(len(service.fail_stuck(now=self.later())), 1)
        self.assertEqual(service.fail_stuck(now=self.later()), [])
        self.assertEqual(status_entries(tender).count(), 1)

    def test_the_deadline_is_configurable(self):
        self.upload()
        self.assertEqual(service.fail_stuck(stuck_after=datetime.timedelta(minutes=45), now=self.later(31)), [])
        self.assertEqual(len(service.fail_stuck(stuck_after=datetime.timedelta(minutes=45), now=self.later(46))), 1)
