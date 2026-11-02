"""
The pipeline end to end, against PostgreSQL: upload -> prepare_extraction -> sections ready to
send -> draft Item rows. Nothing here calls a model. (CLAUDE.md §5-§6.)
"""

import datetime

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase

from core.models import AuditAction, AuditEntry
from tenders.models import (
    ExtractionOutcome as X,
    Tender,
    TenderDocument,
    TenderDocumentKind,
    TenderExtraction,
    TenderStatus as S,
    TransitionError,
)
from tenders.pipeline import service
from tenders.pipeline.converter import PyMuPDF4LLMConverter
from tenders.pipeline.sections import Group as G
from tenders.pipeline.tables import render_markdown, split_pages

from .helpers import TenderFixtures, backdate_updated_at, confirmed_fields, force_status, status_entries
from .pipeline_helpers import (
    GASCO, SAMPLE, TANAPA, CachedConverter, FailingConverter, StaticConverter, blank_pdf, encrypted_pdf,
    fixture_bytes, mini_tender_pages, requires_fixtures, scanned_pdf, text_pdf,
)

PDF = b"%PDF-1.4 placeholder bytes: the StaticConverter never reads them"


class PipelineCase(TenderFixtures, TestCase):
    def upload(self, data=PDF, name="tender.pdf", user=None):
        return Tender.objects.create_from_upload(uploaded_by=user or self.pm, pdf=ContentFile(data, name=name))

    def prepared(self, data=PDF, converter=None):
        tender = self.upload(data)
        service.prepare_extraction(tender, converter=converter or StaticConverter(mini_tender_pages()))
        return tender


class UploadTests(PipelineCase):
    def test_the_pdf_is_stored_as_the_tender_pdf_and_nothing_is_converted_in_the_request(self):
        tender = self.upload(name="gasco.pdf")
        (doc,) = tender.documents.all()
        self.assertEqual((doc.kind, doc.original_filename, doc.uploaded_by), (TenderDocumentKind.TENDER_PDF, "gasco.pdf", self.pm))
        self.assertEqual(doc.file.read(), PDF)
        self.assertEqual(doc.markdown, "")  # conversion is for the cron command (§6), not the request
        self.assertFalse(TenderExtraction.objects.filter(tender=tender).exists())
        self.assertEqual(tender.status, S.EXTRACTING)

    def test_a_file_that_is_not_a_pdf_is_refused_and_creates_no_tender(self):
        for data in (b"just text", b"", b"PK\x03\x04 a zip"):
            with self.subTest(data=data[:8]), self.assertRaises(ValidationError):
                self.upload(data)
        self.assertFalse(Tender.objects.exists())
        self.assertFalse(TenderDocument.objects.exists())

    def test_an_upload_without_a_file_still_works(self):
        tender = Tender.objects.create_from_upload(uploaded_by=self.pm)
        self.assertEqual(tender.documents.count(), 0)

    def test_upload_and_document_are_one_transaction(self):
        with self.assertRaises(ValidationError):
            self.upload(b"nope")
        self.assertEqual(AuditEntry.objects.filter(model="tenders.TenderDocument").count(), 0)


class PrepareHealthyTests(PipelineCase):
    def test_a_readable_tender_is_prepared_and_stays_in_extracting(self):
        converter = StaticConverter(mini_tender_pages())
        tender = self.upload()
        extraction = service.prepare_extraction(tender, converter=converter)
        tender.refresh_from_db()
        self.assertEqual(extraction.outcome, X.PREPARED)
        self.assertEqual(extraction.converter, "static test converter 1")
        self.assertEqual(extraction.message, "")
        self.assertEqual(tender.status, S.EXTRACTING)  # extraction itself has not happened: no model call yet
        self.assertEqual(status_entries(tender).count(), 0)

    def test_the_markdown_is_stored_on_the_tender_pdf_with_page_markers(self):
        tender = self.prepared()
        doc = tender.documents.get()
        self.assertEqual(doc.markdown, render_markdown(sorted(mini_tender_pages().items())))
        self.assertEqual(doc.markdown.count("<!-- page "), 12)
        self.assertEqual(split_pages(doc.markdown), mini_tender_pages())

    def test_the_plan_records_what_was_located_and_that_nothing_needs_the_fallback(self):
        plan = self.prepared().extraction.plan
        self.assertEqual(plan["page_count"], 12)
        self.assertEqual(plan["whole_document_fallback"], [])
        self.assertEqual(set(plan["groups"]), set(G.ALL))
        self.assertEqual(plan["sections"]["GOODS_SCHEDULE"]["pages"], [7, 8])
        self.assertEqual(plan["sections"]["SPECIFICATIONS"]["pages"], [9, 9])
        self.assertTrue(plan["text_layer"]["ok"])
        self.assertEqual(plan["goods_schedule"]["lots"][0]["gfs_code"], "31122139")  # visible to the PM at verification

    def test_it_is_idempotent_and_converts_once(self):
        converter = StaticConverter(mini_tender_pages())
        tender = self.upload()
        first = service.prepare_extraction(tender, converter=converter)
        second = service.prepare_extraction(tender, converter=converter)
        self.assertEqual((first.pk, converter.calls), (second.pk, 1))
        self.assertEqual(TenderExtraction.objects.filter(tender=tender).count(), 1)

    def test_a_tender_being_processed_elsewhere_is_left_alone(self):
        tender = self.upload()
        TenderExtraction.objects.create(tender=tender)  # another cron run has claimed it (PROCESSING)
        converter = StaticConverter(mini_tender_pages())
        result = service.prepare_extraction(tender, converter=converter)
        self.assertEqual((result.outcome, converter.calls), (X.PROCESSING, 0))

    def test_a_claim_left_by_a_crashed_worker_is_taken_over(self):
        tender = self.upload()
        claim = TenderExtraction.objects.create(tender=tender)
        # Backdate the claim: Windows clock ticks are ~15.6ms, so a zero threshold is flaky.
        backdate_updated_at(claim)
        converter = StaticConverter(mini_tender_pages())
        result = service.prepare_extraction(tender, converter=converter)
        self.assertEqual((result.outcome, converter.calls), (X.PREPARED, 1))

    def test_only_a_tender_in_extracting_can_be_prepared(self):
        tender = self.upload()
        force_status(tender, S.PENDING_VERIFICATION)
        with self.assertRaises(TransitionError):
            service.prepare_extraction(tender, converter=StaticConverter(mini_tender_pages()))
        self.assertFalse(TenderExtraction.objects.filter(tender=tender).exists())

    def test_the_audit_trail_records_the_markdown_changed_not_a_copy_of_it(self):
        tender = self.prepared()
        doc = tender.documents.get()
        entry = AuditEntry.objects.filter(model="tenders.TenderDocument", object_id=str(doc.pk),
                                          action=AuditAction.UPDATE).latest("id")
        self.assertEqual(entry.changes["markdown"][1], "[redacted]")
        self.assertLess(len(str(entry.changes)), 500)
        ext = AuditEntry.objects.filter(model="tenders.TenderExtraction", object_id=str(tender.extraction.pk))
        self.assertTrue(all(len(str(e.changes)) < 2000 for e in ext))

    def test_the_pipeline_acts_as_the_system_not_as_a_user(self):
        tender = self.prepared()
        entries = AuditEntry.objects.filter(model="tenders.TenderExtraction", object_id=str(tender.extraction.pk))
        self.assertTrue(entries.exists())
        self.assertTrue(all(e.actor is None for e in entries))


class ManualEntryTests(PipelineCase):
    """§6: a scan produces nothing. Detect it, say so, and never trap the tender (R12)."""

    def assertManual(self, tender, text):
        tender.refresh_from_db()
        extraction = tender.extraction
        self.assertEqual(extraction.outcome, X.MANUAL_ENTRY)
        self.assertIn(text, extraction.message)
        self.assertEqual(tender.status, S.PENDING_VERIFICATION)
        change = status_entries(tender).get()
        self.assertEqual((change.actor, change.changes["status"]), (None, [S.EXTRACTING, S.PENDING_VERIFICATION]))
        return extraction

    def test_a_scanned_pdf_goes_to_manual_entry_with_a_clear_message(self):
        tender = self.upload(scanned_pdf(4))
        service.prepare_extraction(tender, converter=PyMuPDF4LLMConverter())
        extraction = self.assertManual(tender, "scanned")
        self.assertIn("enter the tender details by hand", extraction.message)
        self.assertFalse(extraction.plan["text_layer"]["ok"])
        self.assertEqual(extraction.plan["text_layer"]["pages"], 4)

    def test_a_scan_never_extracts_silently_to_blank_fields(self):
        tender = self.upload(scanned_pdf(3))
        service.prepare_extraction(tender, converter=PyMuPDF4LLMConverter())
        tender.refresh_from_db()
        self.assertNotEqual(tender.extraction.outcome, X.PREPARED)
        self.assertNotIn("sections", tender.extraction.plan)
        with self.assertRaises(TransitionError):
            service.ready_sections(tender)  # nothing to send, and the code says so

    def test_a_blank_pdf_goes_to_manual_entry(self):
        tender = self.upload(blank_pdf(5))
        service.prepare_extraction(tender, converter=PyMuPDF4LLMConverter())
        self.assertManual(tender, "scanned")

    def test_an_unreadable_pdf_goes_to_manual_entry(self):
        tender = self.upload(b"%PDF-1.4 this header is real, the rest is nonsense")
        service.prepare_extraction(tender, converter=PyMuPDF4LLMConverter())
        self.assertManual(tender, "could not be")

    def test_a_password_protected_pdf_goes_to_manual_entry(self):
        tender = self.upload(encrypted_pdf())
        service.prepare_extraction(tender, converter=PyMuPDF4LLMConverter())
        self.assertManual(tender, "password")

    def test_a_tender_with_no_pdf_goes_to_manual_entry(self):
        tender = Tender.objects.create_from_upload(uploaded_by=self.pm)
        service.prepare_extraction(tender, converter=StaticConverter({}))
        self.assertManual(tender, "No tender PDF")

    def test_an_unexpected_failure_goes_to_manual_entry_and_is_logged(self):
        tender = self.upload()
        with self.assertLogs("tenders.pipeline.service", level="ERROR"):
            service.prepare_extraction(tender, converter=FailingConverter(RuntimeError("boom")))
        self.assertManual(tender, "could not be read automatically")

    def test_a_bug_in_planning_goes_to_manual_entry_not_to_a_stuck_tender(self):
        class Weird(StaticConverter):
            def convert(self, pdf_bytes):
                doc = super().convert(pdf_bytes)
                object.__setattr__(doc.pages[1], "markdown", None)  # corrupt a page
                return doc

        tender = self.upload()
        with self.assertLogs("tenders.pipeline.service", level="ERROR"):
            service.prepare_extraction(tender, converter=Weird(mini_tender_pages()))
        self.assertManual(tender, "could not be read automatically")

    def test_the_pm_can_carry_on_from_manual_entry(self):
        tender = self.upload(scanned_pdf(3))
        service.prepare_extraction(tender, converter=PyMuPDF4LLMConverter())
        tender.refresh_from_db()
        tender.confirm_extraction(self.pm, **confirmed_fields())  # the form was empty; the PM types it
        self.assertEqual(tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_a_tender_withdrawn_while_converting_is_not_moved_again(self):
        class Withdrawing(FailingConverter):
            def convert(inner, pdf_bytes):  # noqa: N805
                self_tender.withdraw(self.md)
                raise RuntimeError("late failure")

        tender = self_tender = self.upload()
        with self.assertLogs("tenders.pipeline.service", level="ERROR"):
            service.prepare_extraction(tender, converter=Withdrawing(None))
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.WITHDRAWN)
        self.assertEqual(tender.extraction.outcome, X.MANUAL_ENTRY)


class WholeDocumentFallbackEndToEnd(PipelineCase):
    def test_a_tender_with_unfamiliar_everything_is_prepared_not_failed(self):
        pdf = text_pdf([f"Page {n}: an unfamiliar tender document with no contents page and its own headings." * 3
                        for n in range(1, 6)])
        tender = self.upload(pdf)
        service.prepare_extraction(tender, converter=PyMuPDF4LLMConverter())
        tender.refresh_from_db()
        self.assertEqual(tender.extraction.outcome, X.PREPARED)
        self.assertEqual(tender.status, S.EXTRACTING)
        plan = tender.extraction.plan
        self.assertEqual(set(plan["whole_document_fallback"]), set(G.ALL))
        ready = service.ready_sections(tender)
        self.assertEqual({r.group for r in ready}, set(G.ALL))
        self.assertTrue(all(r.method == "whole_document" and r.ranges == ((1, 5),) for r in ready))
        self.assertTrue(all(r.text.count("<!-- page ") == 5 for r in ready))

    def test_one_unlocatable_section_falls_back_while_the_rest_stay_narrow(self):
        pages = mini_tender_pages()
        pages[9] = "Some other text entirely, no technical requirements table here."
        tender = self.upload()
        service.prepare_extraction(tender, converter=StaticConverter(pages))
        ready = {r.group: r for r in service.ready_sections(Tender.objects.get(pk=tender.pk))}
        self.assertEqual(ready[G.SPECIFICATIONS].method, "whole_document")
        self.assertIn("Specifications .... p.9", ready[G.SPECIFICATIONS].toc_hint)
        self.assertEqual(ready[G.GOODS_SCHEDULE].method, "toc+content")


class ReadySectionsTests(PipelineCase):
    def test_sections_are_built_from_the_stored_markdown(self):
        tender = self.prepared()
        ready = {r.group: r for r in service.ready_sections(tender)}
        self.assertEqual(set(ready), set(G.ALL))
        self.assertEqual(ready[G.GOODS_SCHEDULE].ranges, ((7, 8),))
        self.assertIn("GFS Code: 31122139", ready[G.GOODS_SCHEDULE].text)
        self.assertEqual(ready[G.METADATA].ranges, ((3, 4),))
        self.assertIn("submission deadline", ready[G.METADATA].text)

    def test_the_tender_must_have_been_prepared(self):
        tender = self.upload()
        with self.assertRaises(TransitionError):
            service.ready_sections(tender)


@requires_fixtures(GASCO, SAMPLE, TANAPA)
class RealTendersEndToEnd(PipelineCase):
    converter = CachedConverter()

    def run_pipeline(self, name):
        tender = self.upload(fixture_bytes(name), name=name)
        service.prepare_extraction(tender, converter=self.converter)
        tender.refresh_from_db()
        return tender

    def test_gasco(self):
        tender = self.run_pipeline(GASCO)
        self.assertEqual((tender.extraction.outcome, tender.status), (X.PREPARED, S.EXTRACTING))
        plan = tender.extraction.plan
        self.assertEqual(plan["page_count"], 173)
        self.assertEqual(plan["whole_document_fallback"], [])
        self.assertIn("73", plan["raw_text_pages"])
        ready = {r.group: r for r in service.ready_sections(tender)}
        self.assertEqual(ready[G.METADATA].ranges, ((71, 80), (7, 9)))
        self.assertIn(73, ready[G.METADATA].raw_text_pages)
        self.assertIn("Authorization is required", ready[G.METADATA].text)
        self.assertEqual(plan["goods_schedule"]["lots"][0]["gfs_code"], "31122127")

    def test_the_geological_survey_tender(self):
        tender = self.run_pipeline(SAMPLE)
        plan = tender.extraction.plan
        self.assertEqual(tender.extraction.outcome, X.PREPARED)
        self.assertEqual(plan["goods_schedule"]["lots"][0]["rows"][0]["description"], "Carbon and sulfur analyzer")
        self.assertEqual(plan["goods_schedule"]["lots"][0]["gfs_code"], "31122139")

    def test_tanapa(self):
        tender = self.run_pipeline(TANAPA)
        plan = tender.extraction.plan
        self.assertEqual(plan["groups"][G.METADATA], ["INVITATION"])
        self.assertEqual(plan["raw_text_pages"], {})
        self.assertEqual(plan["goods_schedule"]["lots"][0]["gfs_code"], "31122220")

    def test_the_stored_markdown_is_the_converters_output_with_markers(self):
        tender = self.run_pipeline(TANAPA)
        doc = tender.documents.get()
        self.assertEqual(doc.markdown, self.converter.convert(fixture_bytes(TANAPA)).markdown)
        self.assertEqual(doc.markdown.count("<!-- page "), 73)


class DraftItemsTests(PipelineCase):
    def registered(self, pages=None, status=S.DRAFTING):
        tender = self.upload()
        service.prepare_extraction(tender, converter=StaticConverter(pages or mini_tender_pages()))
        return force_status(tender, status)

    def test_goods_rows_become_draft_items(self):
        tender = self.registered()
        result = service.create_draft_items(tender, actor=self.pm)
        self.assertEqual(len(result.created), 2)
        self.assertEqual(result.skipped, [])
        items = list(tender.items.order_by("item_reference"))
        self.assertEqual([(i.specification, i.quantity, i.unit_of_measure) for i in items],
                         [("Laboratory centrifuge", 2, "No."), ("Rotor, 24 x 1.5 ml", 2, "No.")])
        self.assertEqual([i.item_reference for i in items], [f"{tender.reference}-001", f"{tender.reference}-002"])
        self.assertTrue(all(i.status == "DRAFT" for i in items))
        self.assertTrue(all(i.total_selling_price_tzs is None for i in items))  # drafts are unpriced
        self.assertEqual(result.lots[0].gfs_code, "31122139")

    def test_draft_item_creation_is_audited_to_the_actor(self):
        tender = self.registered()
        service.create_draft_items(tender, actor=self.pm)
        entry = AuditEntry.objects.get(model="items.Item", object_id=str(tender.items.first().pk), action=AuditAction.CREATE)
        self.assertEqual(entry.actor, self.pm)

    def test_it_never_creates_them_twice(self):
        tender = self.registered()
        service.create_draft_items(tender, actor=self.pm)
        again = service.create_draft_items(tender, actor=self.pm)
        self.assertTrue(again.already_had_items)
        self.assertEqual((again.created, tender.items.count()), ([], 2))

    def test_the_pm_can_start_costing_once_draft_items_exist(self):
        tender = self.registered()
        service.create_draft_items(tender, actor=self.pm)
        tender.start_costing(self.pm)  # §5: start_costing needs at least one Item
        self.assertEqual(tender.status, S.COSTING)

    # R15 --------------------------------------------------------------------------------
    def test_r15_no_draft_items_before_the_md_registers_the_tender(self):
        for status in (S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL, S.WITHDRAWN,
                       S.PENDING_AUDIT, S.READY_TO_SUBMIT):
            with self.subTest(status=status):
                extra = {"exchange_rate_locked_at": datetime.datetime.now(datetime.timezone.utc)} if status in (
                    S.PENDING_AUDIT, S.READY_TO_SUBMIT, S.WITHDRAWN) else {}
                tender = self.registered(status=status) if status != S.EXTRACTING else self.prepared()
                if extra:
                    force_status(tender, status, **extra)
                with self.assertRaises(TransitionError) as ctx:
                    service.create_draft_items(tender, actor=self.pm)
                self.assertEqual(ctx.exception.code, "r15")
                self.assertEqual(tender.items.count(), 0)

    # R9a --------------------------------------------------------------------------------
    def test_r9a_no_draft_items_while_the_tender_is_frozen(self):
        tender = self.registered()
        force_status(tender, S.PENDING_ELIGIBILITY_DECISION, previous_status=S.DRAFTING)
        with self.assertRaises(TransitionError) as ctx:
            service.create_draft_items(tender, actor=self.pm)
        self.assertEqual(ctx.exception.code, "r9a")
        self.assertEqual(tender.items.count(), 0)

    # never guess (R12) ---------------------------------------------------------------------
    def test_a_row_whose_quantity_cannot_be_read_is_skipped_and_reported(self):
        tender = self.registered(mini_tender_pages(
            "|1|Centrifuge|No.|2|\n|2|Reagent kit|Kit|2.5|\n|3|Consumables|Lot|ten|\n|4|Rotor|No.|1,200|\n"))
        result = service.create_draft_items(tender, actor=self.pm)
        self.assertEqual([i.specification for i in result.created], ["Centrifuge", "Rotor"])
        self.assertEqual([(r.sn, r.quantity_raw) for r, _ in result.skipped], [("2", "2.5"), ("3", "ten")])
        self.assertTrue(all("not a whole number" in reason for _, reason in result.skipped))
        self.assertEqual(tender.items.count(), 2)
        self.assertEqual(tender.items.get(specification="Rotor").quantity, 1200)

    def test_a_lot_row_stays_one_item(self):
        tender = self.registered(mini_tender_pages("|1|1. Mower A  2. Mower B|Lot|1|\n"))
        result = service.create_draft_items(tender, actor=self.pm)
        (item,) = result.created
        self.assertEqual((item.quantity, item.unit_of_measure), (1, "Lot"))
        self.assertEqual(item.specification, "1. Mower A 2. Mower B")

    def test_an_empty_schedule_creates_nothing_and_blocks_nothing(self):
        tender = self.registered(mini_tender_pages(""))
        result = service.create_draft_items(tender, actor=self.pm)
        self.assertEqual((result.created, result.skipped, tender.items.count()), ([], [], 0))

    def test_a_tender_that_went_to_manual_entry_has_nothing_to_parse(self):
        tender = self.upload(scanned_pdf(3))
        service.prepare_extraction(tender, converter=PyMuPDF4LLMConverter())
        force_status(tender, S.DRAFTING)  # the PM typed it and the MD registered it
        with self.assertRaises(TransitionError):
            service.create_draft_items(tender, actor=self.pm)
        self.assertEqual(tender.items.count(), 0)  # the PM adds items by hand (§5)

    def test_the_tender_must_have_been_prepared(self):
        tender = force_status(self.upload(), S.DRAFTING)
        with self.assertRaises(TransitionError):
            service.create_draft_items(tender, actor=self.pm)


@requires_fixtures(GASCO, SAMPLE, TANAPA)
class DraftItemsFromRealTenders(PipelineCase):
    converter = CachedConverter()

    def draft(self, name):
        tender = self.upload(fixture_bytes(name), name=name)
        service.prepare_extraction(tender, converter=self.converter)
        force_status(tender, S.DRAFTING)
        return tender, service.create_draft_items(tender, actor=self.pm)

    def test_gasco_gives_two_items(self):
        tender, result = self.draft(GASCO)
        self.assertEqual([(i.specification, i.quantity, i.unit_of_measure) for i in result.created],
                         [("Installation of Pressure Reduction Station (PRS)", 3, "No."),
                          ("Supply of Pressure Reduction Station (PRS) Construction", 3, "No.")])
        self.assertEqual(result.lots[0].gfs_code, "31122127")

    def test_the_geological_survey_tender_gives_the_analyzer(self):
        _, result = self.draft(SAMPLE)
        self.assertEqual([(i.specification, i.quantity, i.unit_of_measure) for i in result.created],
                         [("Carbon and sulfur analyzer", 1, "pc")])

    def test_tanapa_gives_one_lot_item_holding_both_machines(self):
        _, result = self.draft(TANAPA)
        (item,) = result.created
        self.assertEqual((item.quantity, item.unit_of_measure), (1, "Lot"))
        self.assertIn("Toro Reel master", item.specification)
        self.assertIn("TORO GREENS MOWER FLEX 2120", item.specification)
        self.assertEqual(result.lots[0].rows[0].sub_items, 2)
