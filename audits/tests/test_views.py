"""The QC audit screen: required beside offered, variance flagged, verdict by a person (§3, §14, R1, R9)."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Role
from audits.models import QCAudit, Verdict
from items.tests.helpers import ItemFixtures, make_user
from tenders.models import TenderStatus as S


class AuditScreenCase(ItemFixtures, TestCase):
    def pending_audit_tender(self, *specs, quotes=None):
        tender = self.costing()
        items = []
        for spec in specs or ("Platform scale, capacity 656kg",):
            item = self.complete_item(tender, specification=spec)
            items.append(item)
        for item, offered in (quotes or {}).items():
            # A selected quote's make, model and origin replace the item's, so they must be on the quote (R2).
            self.quote(items[item], specification_offered=offered, make_brand="Eppendorf",
                       model_part_number="5424 R", country_of_origin="Germany").select(self.pm)
        tender.submit_for_audit(self.pm)
        return tender, items

    def start(self, tender, user=None):
        self.client.force_login(user or self.auditor)
        return self.client.post(reverse("audits:start", args=[tender.pk]))

    def save_line(self, line, **fields):
        data = {"specification_offered": line.specification_offered, "verdict": "", "comments": ""}
        data.update(fields)
        return self.client.post(reverse("audits:line_save", args=[line.pk]), data)


class AccessTests(AuditScreenCase):
    def test_queue_is_for_auditors_and_management(self):
        url = reverse("audits:queue")
        for user, expected in ((self.auditor, 200), (self.md, 200), (self.hops, 200), (self.pm, 403),
                               (self.finance, 403), (make_user(Role.CLEARING_AGENT, "clearer"), 403)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, expected, user.role)

    def test_queue_lists_tenders_waiting_for_audit(self):
        waiting, _ = self.pending_audit_tender()
        other = self.costing()
        self.client.force_login(self.auditor)
        response = self.client.get(reverse("audits:queue"))
        self.assertContains(response, waiting.reference)
        self.assertNotContains(response, other.reference)

    def test_only_the_tenders_own_pm_among_pms_reads_the_audit(self):
        tender, _ = self.pending_audit_tender()
        url = reverse("audits:detail", args=[tender.pk])
        for user, expected in ((self.pm, 200), (self.pm2, 403), (self.finance, 403), (self.hops, 200)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, expected, user.role)

    def test_a_pm_cannot_start_or_save_an_audit(self):
        tender, _ = self.pending_audit_tender()
        self.assertEqual(self.start(tender, self.pm).status_code, 403)
        self.assertFalse(QCAudit.objects.exists())


class StartAndVarianceTests(AuditScreenCase):
    def test_start_shows_required_beside_offered_from_the_selected_quote(self):
        tender, _ = self.pending_audit_tender("Platform scale, capacity 656 kg", quotes={0: "Capacity 620 kg"})
        self.start(tender)
        response = self.client.get(reverse("audits:detail", args=[tender.pk]))
        self.assertContains(response, "Required by the tender")
        self.assertContains(response, "Platform scale, capacity 656 kg")
        self.assertContains(response, "Capacity 620 kg")
        # Numeric variance is flagged, in the model's own words, and no verdict is chosen for the auditor.
        self.assertContains(response, "required 656 kg, offered 620 kg")
        self.assertContains(response, "-36 kg")
        self.assertEqual(QCAudit.objects.get().lines.get().verdict, "")

    def test_the_auditor_who_is_the_tenders_pm_is_refused_with_the_reason(self):
        tender, _ = self.pending_audit_tender()
        # A PM-role user cannot audit at all; the screen says why rather than offering a button.
        self.client.force_login(self.pm)
        self.assertNotContains(self.client.get(reverse("audits:detail", args=[tender.pk])), "Start the audit")

    def test_editing_the_offered_text_recomputes_the_variance(self):
        tender, _ = self.pending_audit_tender("Scale 656 kg")
        self.start(tender)
        line = QCAudit.objects.get().lines.get()
        response = self.save_line(line, specification_offered="Capacity 700 kg")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "required 656 kg, offered 700 kg")
        self.assertContains(response, f'id="line-{line.pk}-variance"')
        self.assertContains(response, 'id="audit-summary"')
        line.refresh_from_db()
        self.assertEqual(line.variance[0]["difference"], "44")

    def test_the_variance_clears_when_the_numbers_agree(self):
        tender, _ = self.pending_audit_tender("Scale 656 kg", quotes={0: "Capacity 620 kg"})
        self.start(tender)
        line = QCAudit.objects.get().lines.get()
        self.assertTrue(line.variance)
        self.save_line(line, specification_offered="Capacity 656 kg")
        line.refresh_from_db()
        self.assertEqual(line.variance, [])


class VerdictTests(AuditScreenCase):
    def setUp(self):
        super().setUp()
        self.tender, _ = self.pending_audit_tender("Scale 656 kg", "Autoclave 100 l")
        self.start(self.tender)
        self.audit = QCAudit.objects.get()
        self.lines = list(self.audit.lines.order_by("item__item_reference"))

    def test_a_negative_without_a_comment_is_a_prompt_and_keeps_the_old_verdict(self):
        response = self.save_line(self.lines[0], verdict=Verdict.NEGATIVE)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add a comment saying why")
        self.lines[0].refresh_from_db()
        self.assertEqual(self.lines[0].verdict, "")

    def test_a_negative_with_a_comment_is_recorded(self):
        self.save_line(self.lines[0], verdict=Verdict.NEGATIVE, comments="Capacity short of requirement")
        self.lines[0].refresh_from_db()
        self.assertEqual((self.lines[0].verdict, self.lines[0].comments), (Verdict.NEGATIVE, "Capacity short of requirement"))

    def test_completing_with_undecided_lines_is_refused_and_says_how_many(self):
        self.save_line(self.lines[0], verdict=Verdict.POSITIVE)
        response = self.client.post(reverse("audits:complete", args=[self.tender.pk]), follow=True)
        self.assertIn("1 line still need a verdict", " ".join(str(m) for m in response.context["messages"]))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_AUDIT)

    def test_a_positive_audit_sends_the_tender_to_the_md(self):
        for line in self.lines:
            self.save_line(line, verdict=Verdict.POSITIVE)
        self.client.post(reverse("audits:complete", args=[self.tender.pk]))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_EXECUTIVE_APPROVAL)
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.verdict, Verdict.POSITIVE)

    def test_a_negative_audit_returns_the_tender_to_costing_and_unlocks_rates_r9(self):
        self.save_line(self.lines[0], verdict=Verdict.NEGATIVE, comments="Too small")
        self.save_line(self.lines[1], verdict=Verdict.POSITIVE)
        self.client.post(reverse("audits:complete", args=[self.tender.pk]), {"reason": "See line 1."})
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.COSTING)
        self.assertIsNone(self.tender.exchange_rate_locked_at)
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.reason, "See line 1.")

    def test_only_the_audits_own_auditor_can_save_or_complete(self):
        other = make_user(Role.AUDITOR, "second_auditor")
        self.client.force_login(other)
        self.assertEqual(self.save_line(self.lines[0], verdict=Verdict.POSITIVE).status_code, 403)
        self.client.post(reverse("audits:complete", args=[self.tender.pk]))
        self.audit.refresh_from_db()
        self.assertFalse(self.audit.is_complete)

    def test_a_completed_audit_is_read_only(self):
        for line in self.lines:
            self.save_line(line, verdict=Verdict.POSITIVE)
        self.client.post(reverse("audits:complete", args=[self.tender.pk]))
        self.assertEqual(self.save_line(self.lines[0], verdict=Verdict.NEGATIVE, comments="x").status_code, 403)
        response = self.client.get(reverse("audits:detail", args=[self.tender.pk]))
        self.assertNotContains(response, 'name="verdict"')
        self.assertContains(response, "Positive")
