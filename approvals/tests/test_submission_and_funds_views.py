"""MD submission approval with signature (R3, R9) and fund requests: raise, approve, mark paid (R17)."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import Role
from approvals.models import Approval, ApprovalDecision, ApprovalKind, FundRequest, FundRequestPurpose, FundRequestStatus
from items.tests.helpers import ItemFixtures, make_user
from tenders.models import Tender, TenderStatus as S
from tenders.tests.helpers import force_status


class SubmissionApprovalTests(ItemFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.tender = self.costing()
        self.complete_item(self.tender, fob_unit_price=Decimal("100"))
        self.tender.submit_for_audit(self.pm)
        force_status(self.tender, S.PENDING_EXECUTIVE_APPROVAL)
        self.url = reverse("approvals:submission_decide", args=[self.tender.pk])

    def sign(self, decision, *, comments="", password="x", typed_name=None):
        return self.client.post(self.url, {
            "decision": decision, "comments": comments, "password": password,
            "typed_name": self.md.get_full_name() if typed_name is None else typed_name,
        })

    def test_only_the_md_sees_or_signs_it(self):
        for user in (self.pm, self.hops, self.auditor, self.finance, self.admin_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("approvals:submission_queue")).status_code, 403, user.role)
            self.assertEqual(self.sign(ApprovalDecision.APPROVED).status_code, 403, user.role)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_EXECUTIVE_APPROVAL)

    def test_the_screen_shows_what_is_being_signed(self):
        self.client.force_login(self.md)
        response = self.client.get(self.url)
        for expected in ("Bid value, excluding VAT", "VAT", "Total tender price", "QC audit", "NeST account", "Sign decision"):
            self.assertContains(response, expected)
        self.assertEqual(response.context["summary"].items_priced, 1)
        self.assertContains(response, "needs-md-panel")  # the MD is the blocker: brass belongs here

    def test_approve_moves_to_ready_to_submit_and_stores_the_typed_name_never_the_password(self):
        self.client.force_login(self.md)
        self.assertRedirects(self.sign(ApprovalDecision.APPROVED), reverse("approvals:submission_queue"))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.READY_TO_SUBMIT)  # R3
        approval = Approval.objects.get(kind=ApprovalKind.SUBMISSION)
        self.assertEqual((approval.decided_by, approval.signature), (self.md, self.md.get_full_name()))

    def test_a_wrong_password_or_name_signs_nothing(self):
        self.client.force_login(self.md)
        self.assertContains(self.sign(ApprovalDecision.APPROVED, password="wrong"), "signature does not verify")
        self.assertContains(self.sign(ApprovalDecision.APPROVED, typed_name="Someone Else"), "signature does not verify")
        self.assertFalse(Approval.objects.exists())

    def test_declining_needs_a_reason_and_returns_to_costing_with_rates_unlocked_r9(self):
        self.client.force_login(self.md)
        self.assertContains(self.sign(ApprovalDecision.DECLINED), "Give the reason")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_EXECUTIVE_APPROVAL)
        self.sign(ApprovalDecision.DECLINED, comments="Margin too thin on lot 2")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.COSTING)
        self.assertIsNone(self.tender.exchange_rate_locked_at)

    def test_a_tender_not_awaiting_submission_is_not_signable(self):
        other = self.costing()
        self.client.force_login(self.md)
        response = self.client.get(reverse("approvals:submission_decide", args=[other.pk]))
        self.assertRedirects(response, reverse("tenders:detail", args=[other.pk]), fetch_redirect_response=False)

    def test_the_queue_lists_only_tenders_waiting_for_the_md(self):
        self.client.force_login(self.md)
        response = self.client.get(reverse("approvals:submission_queue"))
        self.assertContains(response, self.tender.reference)
        self.assertNotContains(response, self.costing().reference)


class FundRequestScreenTests(ItemFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.tender = self.costing()

    def raise_request(self, user=None, **fields):
        self.client.force_login(user or self.pm)
        data = {"purpose": FundRequestPurpose.NEST_APPLICATION_FEE, "amount": "50000", "currency": "TZS", "notes": ""}
        data.update(fields)
        return self.client.post(reverse("approvals:fund_new", args=[self.tender.pk]), data)

    def make_request(self, **fields):
        self.raise_request(**fields)
        return FundRequest.objects.latest("id")

    def test_the_tenders_pm_raises_a_request(self):
        response = self.raise_request()
        fr = FundRequest.objects.get()
        self.assertRedirects(response, reverse("approvals:fund_detail", args=[fr.pk]))
        self.assertEqual((fr.requested_by, fr.status, fr.amount), (self.pm, FundRequestStatus.PENDING, Decimal("50000.00")))

    def test_nobody_else_raises_one(self):
        for user in (self.pm2, self.md, self.finance, self.auditor):
            self.assertEqual(self.raise_request(user).status_code, 403, user.role)
        self.assertFalse(FundRequest.objects.exists())

    def test_a_zero_amount_and_a_closed_tender_are_refused(self):
        self.assertEqual(self.raise_request(amount="0").status_code, 200)
        self.assertFalse(FundRequest.objects.exists())
        force_status(self.tender, S.WITHDRAWN)
        response = self.raise_request()
        self.assertContains(response, "until it is submitted")
        self.assertFalse(FundRequest.objects.exists())

    def test_list_is_scoped_in_the_queryset_a_pm_sees_only_their_own_tenders(self):
        mine = self.make_request()
        other = Tender.objects.create_from_upload(uploaded_by=self.pm2)
        force_status(other, S.COSTING)
        FundRequest.objects.create(tender=other, purpose="OTHER", amount=Decimal("1"), currency="TZS", requested_by=self.pm2)
        self.client.force_login(self.pm)
        response = self.client.get(reverse("approvals:fund_list"))
        self.assertContains(response, mine.tender.reference)
        self.assertNotContains(response, other.reference)
        theirs = FundRequest.objects.get(tender=other)
        self.assertEqual(self.client.get(reverse("approvals:fund_detail", args=[theirs.pk])).status_code, 404)

    def test_who_can_open_the_fund_list(self):
        for user, expected in ((self.pm, 200), (self.md, 200), (self.finance, 200), (self.hops, 200),
                               (self.auditor, 403), (self.admin_user, 403), (make_user(Role.CLEARING_AGENT, "clearer"), 403)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("approvals:fund_list")).status_code, expected, user.role)

    def test_the_md_sees_a_pending_request_as_needing_them_and_only_then_is_brass_used(self):
        fr = self.make_request()
        self.client.force_login(self.md)
        self.assertContains(self.client.get(reverse("approvals:fund_list")), "Needs your approval")
        self.assertContains(self.client.get(reverse("approvals:fund_detail", args=[fr.pk])), "needs-md-panel")
        self.client.force_login(self.finance)
        self.assertNotContains(self.client.get(reverse("approvals:fund_list")), "needs-md-panel")

    def decide(self, fr, **data):
        self.client.force_login(self.md)
        return self.client.post(reverse("approvals:fund_decide", args=[fr.pk]), data, follow=True)

    def test_approve_then_finance_marks_paid_which_funds_the_nest_account_r17(self):
        fr = self.make_request()
        self.assertFalse(self.tender.has_nest_funds)
        self.decide(fr, decision="APPROVE")
        fr.refresh_from_db()
        self.assertEqual(fr.status, FundRequestStatus.APPROVED)
        self.client.force_login(self.finance)
        self.assertContains(self.client.get(reverse("approvals:fund_detail", args=[fr.pk])), "Mark as paid")
        response = self.client.post(reverse("approvals:fund_pay", args=[fr.pk]), {"payment_reference": "NMB-20261102-77"}, follow=True)
        fr.refresh_from_db()
        self.assertEqual((fr.status, fr.paid_by, fr.payment_reference), (FundRequestStatus.PAID, self.finance, "NMB-20261102-77"))
        self.assertIn("NeST account is now funded", " ".join(str(m) for m in response.context["messages"]))
        self.assertTrue(self.tender.has_nest_funds)

    def test_a_paid_bid_security_does_not_fund_nest(self):
        fr = self.make_request(purpose=FundRequestPurpose.BID_SECURITY)
        self.decide(fr, decision="APPROVE")
        self.client.force_login(self.finance)
        self.client.post(reverse("approvals:fund_pay", args=[fr.pk]), {"payment_reference": "REF"})
        self.assertFalse(self.tender.has_nest_funds)

    def test_declining_needs_a_reason(self):
        fr = self.make_request()
        response = self.decide(fr, decision="DECLINE", reason="")
        self.assertIn("Give the reason", " ".join(str(m) for m in response.context["messages"]))
        fr.refresh_from_db()
        self.assertEqual(fr.status, FundRequestStatus.PENDING)
        self.decide(fr, decision="DECLINE", reason="Fee already paid last week")
        fr.refresh_from_db()
        self.assertEqual(fr.status, FundRequestStatus.DECLINED)

    def test_finance_cannot_approve_and_the_md_cannot_pay(self):
        fr = self.make_request()
        self.client.force_login(self.finance)
        self.client.post(reverse("approvals:fund_decide", args=[fr.pk]), {"decision": "APPROVE"})
        fr.refresh_from_db()
        self.assertEqual(fr.status, FundRequestStatus.PENDING)
        self.decide(fr, decision="APPROVE")
        self.client.force_login(self.md)
        self.client.post(reverse("approvals:fund_pay", args=[fr.pk]), {"payment_reference": "X"})
        fr.refresh_from_db()
        self.assertEqual(fr.status, FundRequestStatus.APPROVED)

    def test_payment_needs_a_reference(self):
        fr = self.make_request()
        self.decide(fr, decision="APPROVE")
        self.client.force_login(self.finance)
        self.client.post(reverse("approvals:fund_pay", args=[fr.pk]), {"payment_reference": " "})
        fr.refresh_from_db()
        self.assertEqual(fr.status, FundRequestStatus.APPROVED)
