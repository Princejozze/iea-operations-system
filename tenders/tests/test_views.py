"""The tender screens: list and search (R18), upload (§5.2), detail progress, verification (R12)."""

import datetime
import re
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role
from core.models import Client
from tenders.models import (
    BidSecurityType, Tender, TenderDocument, TenderDocumentKind, TenderExtraction, TenderStatus as S,
)
from tenders.views import blocking_gate

from .helpers import TenderFixtures, add_item, confirmed_fields, force_status, make_user

PDF = b"%PDF-1.4 placeholder bytes"


def _record(value, confidence=0.9, page=3, quote=None, low=False):
    return {"value": value, "confidence": confidence, "source_page": page, "quote": quote,
            "flags": [], "low_confidence": low, "source": "model"}


class ViewTestBase(TenderFixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.clear = make_user(Role.CLEARING_AGENT, "dinna")


class AccessTests(ViewTestBase):
    def test_login_required(self):
        response = self.client.get(reverse("tenders:list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_clearing_agent_cannot_see_tenders(self):
        """R5: enforced in the view's queryset, not by hiding links."""
        tender = self.new_tender()
        self.client.force_login(self.clear)
        self.assertEqual(self.client.get(reverse("tenders:list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("tenders:detail", args=[tender.pk])).status_code, 403)

    def test_other_roles_can_read(self):
        tender = self.new_tender()
        for user in (self.md, self.hops, self.pm2, self.auditor, self.finance):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("tenders:detail", args=[tender.pk])).status_code, 200, user.role)


class SearchTests(ViewTestBase):
    """R18: reference, legacy reference, official number, client and title."""

    def setUp(self):
        self.client.force_login(self.hops)
        self.tender = self.new_tender(S.DRAFTING, **confirmed_fields())
        self.tender.legacy_reference = "KEA-0099-OLD"
        self.tender.save()
        tanapa = Client.objects.create(name="Tanzania National Parks", short_name="TANAPA")
        self.other = self.new_tender(
            S.DRAFTING, **{**confirmed_fields(client=tanapa), "title": "Tractor mowers", "official_reference": "TNP/9"}
        )

    def found(self, q):
        response = self.client.get(reverse("tenders:list"), {"q": q})
        self.assertEqual(response.status_code, 200)
        return {t.pk for t in response.context["page"]}

    def test_each_searchable_field(self):
        for q in (self.tender.reference, "kea-0099", "MNH/2026/G/017", "Muhimbili", "MNH", "centrifuges"):
            self.assertEqual(self.found(q), {self.tender.pk}, q)

    def test_empty_query_lists_all(self):
        self.assertEqual(self.found(""), {self.tender.pk, self.other.pk})

    def test_no_match(self):
        self.assertEqual(self.found("nothing-like-this"), set())

    def test_htmx_returns_rows_only(self):
        response = self.client.get(reverse("tenders:list"), {"q": "tractor"}, HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "<html")
        self.assertContains(response, self.other.reference)
        self.assertNotContains(response, self.tender.reference)

    def test_items_priced_column(self):
        add_item(self.tender)
        add_item(self.tender, unit_cost=100)
        response = self.client.get(reverse("tenders:list"), {"q": self.tender.reference})
        row = next(iter(response.context["page"]))
        self.assertEqual((row.items_priced, row.items_total), (1, 2))


class UploadTests(ViewTestBase):
    def test_pm_upload_creates_extracting_tender_and_returns(self):
        self.client.force_login(self.pm)
        response = self.client.post(
            reverse("tenders:upload"), {"pdf": SimpleUploadedFile("tender.pdf", PDF, "application/pdf")}
        )
        tender = Tender.objects.get()
        self.assertRedirects(response, reverse("tenders:detail", args=[tender.pk]), fetch_redirect_response=False)
        self.assertEqual(tender.status, S.EXTRACTING)
        self.assertEqual(tender.project_manager, self.pm)
        self.assertRegex(tender.reference, r"^IEA-\d{4}-\d{4}$")
        doc = TenderDocument.objects.get(tender=tender)
        self.assertEqual(doc.kind, TenderDocumentKind.TENDER_PDF)
        # §6: nothing is extracted in the request.
        self.assertFalse(TenderExtraction.objects.filter(tender=tender).exists())

    def test_non_pdf_is_refused(self):
        self.client.force_login(self.pm)
        response = self.client.post(
            reverse("tenders:upload"), {"pdf": SimpleUploadedFile("x.pdf", b"hello", "application/pdf")}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not a PDF")
        self.assertFalse(Tender.objects.exists())

    def test_only_pm_may_upload(self):
        for user in (self.md, self.hops, self.auditor, self.finance, self.admin_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("tenders:upload")).status_code, 403, user.role)


class DetailTests(ViewTestBase):
    def test_extracting_polls_then_refreshes(self):
        tender = self.new_tender()
        self.client.force_login(self.pm)
        page = self.client.get(reverse("tenders:detail", args=[tender.pk]))
        self.assertContains(page, reverse("tenders:status", args=[tender.pk]))
        self.assertContains(page, "one to two minutes")
        poll = self.client.get(reverse("tenders:status", args=[tender.pk]))
        self.assertEqual(poll.status_code, 200)
        tender.finish_extraction()
        poll = self.client.get(reverse("tenders:status", args=[tender.pk]))
        self.assertEqual(poll.status_code, 204)
        self.assertEqual(poll["HX-Refresh"], "true")

    def test_progress_figures(self):
        fields = confirmed_fields()
        fields["submission_deadline"] = timezone.now() + datetime.timedelta(days=10)
        fields["clarification_deadline"] = None
        tender = self.new_tender(S.COSTING, **fields)
        add_item(tender)
        add_item(tender, unit_cost=50)
        self.client.force_login(self.hops)
        response = self.client.get(reverse("tenders:detail", args=[tender.pk]))
        self.assertContains(response, tender.reference)
        self.assertEqual((response.context["items_priced"], response.context["items_total"]), (1, 2))
        self.assertIn(response.context["days_left"], (9, 10))  # depends on the hour of the local day
        gate = response.context["gate"]
        self.assertEqual(gate.who, "PM")
        self.assertIn("1 item(s) unpriced", gate.text)
        self.assertTrue(any("(R2)" in d for d in gate.details))

    def test_brass_only_when_the_md_is_needed(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        self.client.force_login(self.md)
        response = self.client.get(reverse("tenders:detail", args=[tender.pk]))
        self.assertTrue(response.context["gate"].needs_md)
        self.assertContains(response, "needs-md-panel")
        self.assertContains(response, reverse("approvals:registration_decide", args=[tender.pk]))

        drafting = self.new_tender(S.DRAFTING, **confirmed_fields())
        self.client.force_login(self.pm)
        response = self.client.get(reverse("tenders:detail", args=[drafting.pk]))
        self.assertNotIn("needs-md", response.content.decode())

    def test_gates(self):
        t = self.new_tender(S.READY_TO_SUBMIT, **confirmed_fields(), exchange_rate_locked_at=timezone.now())
        self.assertEqual(blocking_gate(t).who, "Finance")  # R17: unfunded
        self.assertIn("R17", blocking_gate(t).text)
        for status in (S.PENDING_EXECUTIVE_APPROVAL, S.PENDING_ELIGIBILITY_DECISION):
            self.assertTrue(blocking_gate(Tender(status=status)).needs_md, status)
        self.assertEqual(blocking_gate(Tender(status=S.WON)).text, "")


class VerificationTests(ViewTestBase):
    def setUp(self):
        self.tender = self.new_tender()
        self.mnh = Client.objects.create(name="Muhimbili National Hospital", short_name="MNH")
        TenderExtraction.objects.create(
            tender=self.tender, outcome="PARTIAL",
            fields={
                "official_reference": _record("MNH/2026/G/017", 0.95, 2, "Tender No. MNH/2026/G/017"),
                "title": _record("Supply of laboratory centrifuges", 0.4, 1, low=True),
                "client_name": _record("muhimbili national hospital", 0.9, 1),
                "submission_deadline": _record("2026-12-13T10:00:00", 0.9, 36, "13/12/2026 at 10:00"),
                "clarification_deadline": _record(None, None, None),
                "bid_security_type": _record(BidSecurityType.BANK_GUARANTEE, 0.8, 37),
                "site_visit_required": _record(False, 0.8, 38, "No site visit"),
                "special_requirements": _record([{"text": "Manufacturer's Authorization", "source_page": 73}]),
                "required_forms": {**_record([{"text": "Power of Attorney", "source_page": 90}], 0.95, None),
                                   "source": "toc"},
            },
        )
        self.tender.finish_extraction()
        self.url = reverse("tenders:verify", args=[self.tender.pk])

    def post_data(self, **overrides):
        data = {
            "official_reference": "MNH/2026/G/017", "title": "Supply of laboratory centrifuges",
            "client": self.mnh.pk, "new_client_name": "", "client_address": "P.O. Box 65000",
            "submission_deadline": "2026-12-13T10:00", "clarification_deadline": "",
            "bid_security_type": BidSecurityType.BANK_GUARANTEE, "site_visit_required": "no",
            "site_visit_date": "", "special_requirements": "Manufacturer's Authorization\n\n",
            "required_forms": "Power of Attorney", "category": "",
        }
        data.update(overrides)
        return data

    def test_shows_draft_with_confidence_and_source_page(self):
        self.client.force_login(self.pm)
        response = self.client.get(self.url)
        self.assertContains(response, "Confidence 95%")
        self.assertContains(response, "page 36")
        self.assertContains(response, "Low confidence")
        self.assertContains(response, "from the table of contents")
        self.assertContains(response, "13/12/2026 at 10:00")
        form = response.context["form"]
        self.assertEqual(form.initial["client"], self.mnh.pk)  # case-insensitive match, not a new client
        self.assertEqual(form.initial["site_visit_required"], "no")
        self.assertEqual(form.initial["submission_deadline"], datetime.datetime(2026, 12, 13, 10, 0))
        self.assertIn("Manufacturer's Authorization", form.initial["special_requirements"])
        # R12: nothing has reached the tender yet.
        self.tender.refresh_from_db()
        self.assertEqual((self.tender.title, self.tender.official_reference), ("", ""))

    def test_confirm_moves_to_registration_approval(self):
        self.client.force_login(self.pm)
        response = self.client.post(self.url, self.post_data(title="Laboratory centrifuges (corrected)"))
        self.assertRedirects(response, reverse("tenders:detail", args=[self.tender.pk]), fetch_redirect_response=False)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_REGISTRATION_APPROVAL)
        self.assertEqual(self.tender.title, "Laboratory centrifuges (corrected)")
        self.assertEqual(self.tender.client, self.mnh)
        self.assertIs(self.tender.site_visit_required, False)
        self.assertEqual(self.tender.special_requirements, "Manufacturer's Authorization")
        self.assertEqual(timezone.localtime(self.tender.submission_deadline).hour, 10)

    def test_missing_required_fields_stay_on_form(self):
        self.client.force_login(self.pm)
        response = self.client.post(self.url, self.post_data(title="", site_visit_required="", client=""))
        self.assertEqual(response.status_code, 200)
        errors = response.context["form"].errors
        self.assertIn("title", errors)
        self.assertIn("site_visit_required", errors)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_VERIFICATION)

    def test_new_client_created_only_on_successful_confirmation(self):
        self.client.force_login(self.pm)
        refused = self.post_data(client="", new_client_name="Mkwawa University", title="")
        self.client.post(self.url, refused)
        self.assertFalse(Client.objects.filter(name="Mkwawa University").exists())
        self.client.post(self.url, self.post_data(client="", new_client_name="Mkwawa University"))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.client.name, "Mkwawa University")
        self.assertEqual(self.tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_existing_client_name_is_reused(self):
        self.client.force_login(self.pm)
        self.client.post(self.url, self.post_data(client="", new_client_name="MUHIMBILI national hospital"))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.client, self.mnh)
        self.assertEqual(Client.objects.count(), 1)

    def test_clarification_after_submission_is_reported(self):
        self.client.force_login(self.pm)
        response = self.client.post(self.url, self.post_data(clarification_deadline="2026-12-20T10:00"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("clarification_deadline", response.context["form"].errors)

    def test_only_this_tenders_pm(self):
        for user in (self.pm2, self.md, self.hops):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.url, self.post_data()).status_code, 403, user.role)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_VERIFICATION)

    def test_failed_extraction_still_gives_a_completable_form(self):
        """R12: a failed extraction never traps the tender."""
        tender = self.new_tender()
        TenderExtraction.objects.create(tender=tender, outcome="FAILED", message="Scanned document.")
        tender.finish_extraction()
        self.client.force_login(self.pm)
        url = reverse("tenders:verify", args=[tender.pk])
        self.assertContains(self.client.get(url), "Scanned document.")
        self.client.post(url, self.post_data())
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_not_in_verification_redirects(self):
        tender = self.new_tender(S.DRAFTING, **confirmed_fields())
        self.client.force_login(self.pm)
        response = self.client.get(reverse("tenders:verify", args=[tender.pk]))
        self.assertRedirects(response, reverse("tenders:detail", args=[tender.pk]), fetch_redirect_response=False)


class StaticAssetTests(TestCase):
    """CLAUDE.md §1: Bootstrap and htmx are self-hosted — never a CDN link."""

    def test_no_external_assets_in_templates(self):
        pattern = re.compile(r"""<(?:script|link)[^>]+(?:src|href)=["'](?:https?:)?//""", re.I)
        for directory in settings.TEMPLATES[0]["DIRS"]:
            for path in Path(directory).rglob("*.html"):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path)
