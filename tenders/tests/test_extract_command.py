"""
The cron-invoked command (CLAUDE.md §4/§6): picks up tenders in EXTRACTING, safe for overlapping
runs, and never leaves a tender trapped. No API calls: the extractor is a fake.
"""

import os
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.test import TestCase

from tenders.models import ExtractionOutcome as X, Tender, TenderExtraction, TenderStatus as S
from tenders.pipeline import service
from tenders.pipeline.extractor import ExtractionConfigError

from .helpers import TenderFixtures, backdate_updated_at, force_status
from .pipeline_helpers import FakeExtractor, StaticConverter, good_answer, mini_tender_pages

PDF = b"%PDF-1.4 placeholder bytes: the StaticConverter never reads them"
FROM_ENV = "tenders.management.commands.extract_tenders.GeminiExtractor.from_environment"
CONVERTER = "tenders.pipeline.service.PyMuPDF4LLMConverter"


class CommandCase(TenderFixtures, TestCase):
    def upload(self):
        return Tender.objects.create_from_upload(uploaded_by=self.pm, pdf=ContentFile(PDF, name="t.pdf"))

    def run_command(self, *args, fake=None, **kw):
        fake = fake or FakeExtractor(good_answer())
        out, err = StringIO(), StringIO()
        with mock.patch(FROM_ENV, return_value=fake), mock.patch(CONVERTER, side_effect=lambda: StaticConverter(mini_tender_pages())):
            call_command("extract_tenders", *args, stdout=out, stderr=err, **kw)
        return out.getvalue(), err.getvalue(), fake

    def status(self, tender):
        tender.refresh_from_db()
        return tender.status


class PickingUpTenders(CommandCase):
    def test_it_reads_waiting_tenders_and_moves_them_to_pending_verification(self):
        a, b = self.upload(), self.upload()
        out, _, fake = self.run_command()
        self.assertEqual([self.status(a), self.status(b)], [S.PENDING_VERIFICATION, S.PENDING_VERIFICATION])
        self.assertEqual(fake.calls, 2)
        self.assertIn(f"{a.reference}: EXTRACTED -> PENDING_VERIFICATION", out)
        self.assertIn("2 tender(s) worked on", out)

    def test_the_oldest_tender_goes_first_and_limit_bounds_a_run(self):
        first, second, third = self.upload(), self.upload(), self.upload()
        out, _, _ = self.run_command("--limit", "1")
        self.assertEqual([self.status(t) for t in (first, second, third)], [S.PENDING_VERIFICATION, S.EXTRACTING, S.EXTRACTING])
        self.run_command("--limit", "1")  # the backlog drains over successive runs
        self.assertEqual([self.status(t) for t in (first, second, third)],
                         [S.PENDING_VERIFICATION, S.PENDING_VERIFICATION, S.EXTRACTING])

    def test_limit_zero_does_no_work(self):
        tender = self.upload()
        out, _, fake = self.run_command("--limit", "0")
        self.assertEqual((fake.calls, self.status(tender)), (0, S.EXTRACTING))
        self.assertIn("0 tender(s) worked on", out)

    def test_only_tenders_in_extracting_are_considered(self):
        done = self.new_tender(S.PENDING_VERIFICATION)
        drafting = self.new_tender(S.DRAFTING)
        _, _, fake = self.run_command()
        self.assertEqual((fake.calls, self.status(done), self.status(drafting)), (0, S.PENDING_VERIFICATION, S.DRAFTING))

    def test_a_run_with_nothing_to_do_is_quiet_and_succeeds(self):
        out, err, _ = self.run_command()
        self.assertIn("0 tender(s) worked on", out)
        self.assertEqual(err, "")

    def test_running_again_does_not_touch_a_finished_tender(self):
        tender = self.upload()
        self.run_command()
        _, _, fake = self.run_command()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self.status(tender), S.PENDING_VERIFICATION)

    def test_a_scanned_tender_is_sent_to_manual_entry_without_a_model_call(self):
        from tenders.tests.pipeline_helpers import scanned_pdf
        from tenders.pipeline.converter import PyMuPDF4LLMConverter

        tender = Tender.objects.create_from_upload(uploaded_by=self.pm, pdf=ContentFile(scanned_pdf(3), name="scan.pdf"))
        fake = FakeExtractor(good_answer())
        with mock.patch(FROM_ENV, return_value=fake), mock.patch(CONVERTER, side_effect=PyMuPDF4LLMConverter):
            call_command("extract_tenders", stdout=StringIO(), stderr=StringIO())
        tender.refresh_from_db()
        self.assertEqual((fake.calls, tender.extraction.outcome, tender.status), (0, X.MANUAL_ENTRY, S.PENDING_VERIFICATION))


class OverlappingRuns(CommandCase):
    def test_a_second_run_started_while_the_first_is_calling_the_model_does_not_repeat_the_call(self):
        tender = self.upload()
        inner_out = {}

        def second_run_starts_now(request):
            out = StringIO()
            call_command("extract_tenders", stdout=out, stderr=StringIO())
            inner_out["text"] = out.getvalue()

        fake = FakeExtractor(good_answer(), on_call=second_run_starts_now)
        self.run_command(fake=fake)
        self.assertEqual(fake.calls, 1)  # one model call in total, though two runs overlapped
        self.assertIn("0 tender(s) worked on", inner_out["text"])  # the inner run found the claim and left it
        self.assertEqual(self.status(tender), S.PENDING_VERIFICATION)

    def test_a_second_run_started_while_the_first_is_converting_does_not_convert_again(self):
        tender = self.upload()
        converted = []

        class Slow(StaticConverter):
            def convert(inner, data):  # noqa: N805
                converted.append(1)
                if len(converted) == 1:
                    out = StringIO()
                    with mock.patch(FROM_ENV, return_value=FakeExtractor(good_answer())):
                        call_command("extract_tenders", stdout=out, stderr=StringIO())  # the overlapping run
                return super().convert(data)

        fake = FakeExtractor(good_answer())
        with mock.patch(FROM_ENV, return_value=fake), mock.patch(CONVERTER, side_effect=lambda: Slow(mini_tender_pages())):
            call_command("extract_tenders", stdout=StringIO(), stderr=StringIO())
        self.assertEqual((len(converted), fake.calls), (1, 1))
        self.assertEqual(self.status(tender), S.PENDING_VERIFICATION)

    def test_a_claim_left_by_a_crashed_run_is_picked_up_once_it_is_stale(self):
        tender = self.upload()
        claim = TenderExtraction.objects.create(tender=tender)  # PROCESSING, "crashed"
        _, _, fake = self.run_command()
        self.assertEqual(fake.calls, 0)  # fresh claim: someone may still be on it
        # Backdate the claim rather than relying on the clock advancing (Windows ticks ~15.6ms).
        backdate_updated_at(claim)
        with mock.patch.object(service, "STALE_AFTER", timedelta(minutes=5)):
            _, _, fake = self.run_command()
        self.assertEqual((fake.calls, self.status(tender)), (1, S.PENDING_VERIFICATION))


class OneBadTenderDoesNotStopTheRest(CommandCase):
    def test_an_error_on_one_tender_is_reported_and_the_others_are_still_processed(self):
        bad, good = self.upload(), self.upload()
        real = service.process_tender

        def flaky(tender, **kw):
            if tender.pk == bad.pk:
                raise RuntimeError("disk full")
            return real(tender, **kw)

        with mock.patch.object(service, "process_tender", side_effect=flaky), self.assertLogs("tenders.management.commands.extract_tenders", level="ERROR"):
            out, err, _ = self.run_command()
        self.assertIn(f"{bad.reference}: error", err)
        self.assertEqual((self.status(good), self.status(bad)), (S.PENDING_VERIFICATION, S.EXTRACTING))

    def test_the_tender_that_errored_is_finished_by_the_sweep_later(self):
        bad = self.upload()
        with mock.patch.object(service, "process_tender", side_effect=RuntimeError("x")), self.assertLogs(level="ERROR"):
            self.run_command()
        self.assertEqual(self.status(bad), S.EXTRACTING)
        self.run_command("--stuck-after", "0")
        self.assertEqual(self.status(bad), S.PENDING_VERIFICATION)


class NeverTrapped(CommandCase):
    def test_the_sweep_finishes_a_tender_stuck_past_the_deadline_before_any_other_work(self):
        tender = self.upload()
        out, _, fake = self.run_command("--stuck-after", "0")
        tender.refresh_from_db()
        self.assertEqual((tender.status, tender.extraction.outcome, fake.calls), (S.PENDING_VERIFICATION, X.FAILED, 0))
        self.assertIn(f"{tender.reference}: still extracting after 0 min", out)

    def test_a_missing_api_key_or_model_fails_loudly_with_an_instruction(self):
        self.upload()
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GEMINI_MODEL", None)
            with self.assertRaises(CommandError) as ctx:
                call_command("extract_tenders", stdout=StringIO(), stderr=StringIO())
        self.assertIn("GEMINI_MODEL", str(ctx.exception) + "GEMINI_MODEL")  # names what to set
        self.assertIn("stay in EXTRACTING until this is fixed", str(ctx.exception))

    def test_even_when_misconfigured_no_tender_is_trapped_past_the_deadline(self):
        tender = self.upload()
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GEMINI_MODEL", None)
            with self.assertRaises(CommandError):
                call_command("extract_tenders", "--stuck-after", "0", stdout=StringIO(), stderr=StringIO())
        tender.refresh_from_db()
        self.assertEqual((tender.status, tender.extraction.outcome), (S.PENDING_VERIFICATION, X.FAILED))

    def test_before_the_deadline_a_misconfigured_run_leaves_tenders_waiting_for_the_fix(self):
        tender = self.upload()
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GEMINI_MODEL", None)
            with self.assertRaises(CommandError):
                call_command("extract_tenders", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(self.status(tender), S.EXTRACTING)
        self.run_command()  # configuration fixed
        self.assertEqual(self.status(tender), S.PENDING_VERIFICATION)

    def test_a_configuration_error_raised_mid_run_stops_the_run_and_releases_the_claim(self):
        tender = self.upload()
        fake = FakeExtractor(error=ExtractionConfigError("GEMINI_API_KEY was revoked"))
        with self.assertRaises(CommandError):
            self.run_command(fake=fake)
        tender.refresh_from_db()
        self.assertEqual((tender.status, tender.extraction.outcome), (S.EXTRACTING, X.PREPARED))
