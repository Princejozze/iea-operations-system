"""The MD's registration approval screen (§5 step 8, R15), signed per §4."""

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from approvals.models import Approval, ApprovalDecision, ApprovalKind
from tenders.models import TenderStatus as S
from tenders.tests.helpers import TenderFixtures, confirmed_fields


class RegistrationScreenTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        self.url = reverse("approvals:registration_decide", args=[self.tender.pk])

    def sign(self, decision, *, comments="", password="x", typed_name=None):
        return self.client.post(self.url, {
            "decision": decision, "comments": comments, "password": password,
            "typed_name": self.md.get_full_name() if typed_name is None else typed_name,
        })

    def test_only_the_md(self):
        for user in (self.pm, self.hops, self.auditor, self.finance, self.admin_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("approvals:registration_queue")).status_code, 403, user.role)
            self.assertEqual(self.sign(ApprovalDecision.APPROVED).status_code, 403, user.role)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_queue_lists_pending_only(self):
        drafting = self.new_tender(S.DRAFTING, **confirmed_fields())
        self.client.force_login(self.md)
        response = self.client.get(reverse("approvals:registration_queue"))
        self.assertContains(response, self.tender.reference)
        self.assertNotContains(response, drafting.reference)

    def test_approve(self):
        self.client.force_login(self.md)
        response = self.sign(ApprovalDecision.APPROVED)
        self.assertRedirects(response, reverse("approvals:registration_queue"), fetch_redirect_response=False)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.DRAFTING)
        approval = Approval.objects.get(tender=self.tender)
        self.assertEqual((approval.kind, approval.decided_by), (ApprovalKind.REGISTRATION, self.md))
        self.assertEqual(approval.signature, self.md.get_full_name())

    def test_decline_needs_a_reason(self):
        self.client.force_login(self.md)
        response = self.sign(ApprovalDecision.DECLINED)
        self.assertEqual(response.status_code, 200)
        self.assertIn("comments", response.context["form"].errors)
        self.assertFalse(Approval.objects.exists())

    def test_decline_withdraws_and_tells_the_pm_why(self):
        self.client.force_login(self.md)
        with self.captureOnCommitCallbacks(execute=True):
            self.sign(ApprovalDecision.DECLINED, comments="Outside our product range.")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.WITHDRAWN)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Outside our product range.", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, [self.pm.email])

    def test_bad_signature_records_nothing(self):
        self.client.force_login(self.md)
        for kwargs in ({"password": "wrong"}, {"typed_name": "Someone Else"}):
            response = self.sign(ApprovalDecision.APPROVED, **kwargs)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "signature does not verify")
        self.assertFalse(Approval.objects.exists())
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_password_is_not_echoed_back(self):
        self.client.force_login(self.md)
        response = self.sign(ApprovalDecision.APPROVED, password="wrong-secret")
        self.assertNotContains(response, "wrong-secret")

    def test_tender_not_pending_redirects(self):
        drafting = self.new_tender(S.DRAFTING, **confirmed_fields())
        self.client.force_login(self.md)
        response = self.client.get(reverse("approvals:registration_decide", args=[drafting.pk]))
        self.assertRedirects(response, reverse("tenders:detail", args=[drafting.pk]), fetch_redirect_response=False)
