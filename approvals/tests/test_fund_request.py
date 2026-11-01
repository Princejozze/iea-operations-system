"""
FundRequest (§7) and R17: no submission without NeST funds.

R17's property is true only for a PAID request whose purpose is
NEST_APPLICATION_FEE. Every other combination is tested to fail.
"""

from decimal import Decimal

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from approvals.models import FundRequest, FundRequestPurpose as P, FundRequestStatus as FS
from core.models import AuditBypassError, AuditEntry
from tenders.models import Currency, GuardedFieldError, NEST_FUNDS_PURPOSE, TenderStatus as S, TransitionError

from .helpers import TenderFixtures, fund_request, paid_fund_request, status_entries


class FundRequestLifecycleTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.DRAFTING)

    def test_pm_raises_a_pending_request_and_the_md_is_told(self):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            fr = fund_request(self.tender, self.pm)
        self.assertEqual(fr.status, FS.PENDING)
        self.assertEqual(fr.requested_by, self.pm)
        self.assertEqual(fr.amount, Decimal("50000.00"))
        self.assertEqual([m.to for m in mail.outbox], [[self.md.email]])

    def test_only_this_tenders_pm_can_raise_one(self):
        for user in (self.md, self.hops, self.auditor, self.finance, self.admin_user, self.pm2):
            with self.subTest(role=user.role, who=user.first_name), self.assertRaises(ValidationError):
                fund_request(self.tender, user)
        self.assertFalse(FundRequest.objects.exists())

    def test_a_request_needs_a_registered_live_tender(self):
        for status in (S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL,
                       S.SUBMITTED, S.WON, S.LOST, S.WITHDRAWN):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                fund_request(self.new_tender(status), self.pm)
        for status in (S.DRAFTING, S.COSTING, S.PENDING_ELIGIBILITY_DECISION, S.READY_TO_SUBMIT):
            with self.subTest(status=status):
                extra = {"previous_status": S.DRAFTING} if status == S.PENDING_ELIGIBILITY_DECISION else {}
                fund_request(self.new_tender(status, **extra), self.pm)

    def test_the_amount_must_be_positive(self):
        for amount in ("0", "-5"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                fund_request(self.tender, self.pm, amount=amount)

    def test_md_approves_then_finance_pays(self):
        fr = fund_request(self.tender, self.pm)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            fr.approve(self.md)
        self.assertEqual((fr.status, fr.approved_by), (FS.APPROVED, self.md))
        self.assertIsNotNone(fr.approved_at)
        self.assertCountEqual(mail.outbox[0].to, [self.pm.email, self.finance.email])
        fr.mark_paid(self.finance, "  TZ-2026-77  ")
        self.assertEqual((fr.status, fr.paid_by, fr.payment_reference), (FS.PAID, self.finance, "TZ-2026-77"))
        self.assertIsNotNone(fr.paid_at)

    def test_only_the_right_role_moves_each_step(self):
        fr = fund_request(self.tender, self.pm)
        for user in (self.pm, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(step="approve", role=user.role), self.assertRaises(PermissionDenied):
                fr.approve(user)
        fr.approve(self.md)
        for user in (self.md, self.pm, self.hops, self.auditor, self.admin_user):
            with self.subTest(step="pay", role=user.role), self.assertRaises(PermissionDenied):
                fr.mark_paid(user, "REF")
        fr.refresh_from_db()
        self.assertEqual(fr.status, FS.APPROVED)

    def test_the_steps_run_in_order(self):
        fr = fund_request(self.tender, self.pm)
        with self.assertRaises(TransitionError):
            fr.mark_paid(self.finance, "REF")  # not approved yet
        fr.approve(self.md)
        with self.assertRaises(TransitionError):
            fr.approve(self.md)  # already approved
        with self.assertRaises(TransitionError):
            fr.decline(self.md, "Changed my mind")
        fr.mark_paid(self.finance, "REF")
        with self.assertRaises(TransitionError):
            fr.mark_paid(self.finance, "REF-2")

    def test_decline_needs_a_reason_and_cannot_be_paid(self):
        fr = fund_request(self.tender, self.pm)
        with self.assertRaises(ValidationError):
            fr.decline(self.md, "  ")
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            fr.decline(self.md, "Not budgeted this month.")
        self.assertEqual(fr.status, FS.DECLINED)
        self.assertIn("Not budgeted this month.", fr.notes)
        self.assertEqual([m.to for m in mail.outbox], [[self.pm.email]])
        with self.assertRaises(TransitionError):
            fr.mark_paid(self.finance, "REF")

    def test_payment_needs_a_reference(self):
        fr = fund_request(self.tender, self.pm)
        fr.approve(self.md)
        with self.assertRaises(ValidationError):
            fr.mark_paid(self.finance, " ")
        fr.refresh_from_db()
        self.assertEqual(fr.status, FS.APPROVED)

    def test_status_is_written_only_by_the_transition_methods(self):
        fr = fund_request(self.tender, self.pm)
        fr.status = FS.PAID
        with self.assertRaises(GuardedFieldError):
            fr.save()
        with self.assertRaises(AuditBypassError):
            FundRequest.objects.filter(pk=fr.pk).update(status=FS.PAID)
        with self.assertRaises(GuardedFieldError):
            FundRequest(tender=self.tender, purpose=P.OTHER, amount=1, currency=Currency.TZS,
                        requested_by=self.pm, status=FS.PAID).save()
        fr.refresh_from_db()
        self.assertEqual(fr.status, FS.PENDING)

    def test_the_database_refuses_a_status_that_contradicts_its_columns(self):
        fr = fund_request(self.tender, self.pm)
        from django.db import IntegrityError, transaction

        fr.status = FS.PAID
        with self.assertRaises(IntegrityError), transaction.atomic():
            with fr._system_write():  # bypass the model guard to prove the constraint stands alone
                fr.save()

    def test_every_change_is_audited(self):
        fr = paid_fund_request(self, self.tender)
        entries = AuditEntry.objects.filter(model="approvals.FundRequest", object_id=str(fr.pk)).order_by("id")
        self.assertEqual([e.changes.get("status", [None, None])[1] for e in entries], [FS.PENDING, FS.APPROVED, FS.PAID])
        self.assertEqual([e.actor for e in entries], [self.pm, self.md, self.finance])


class R17FundsGateTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.READY_TO_SUBMIT)

    def assertRefused(self):
        self.tender.refresh_from_db()
        self.assertFalse(self.tender.has_nest_funds)
        with self.assertRaises(TransitionError) as ctx:
            self.tender.mark_submitted(self.pm)
        self.assertIn("R17", str(ctx.exception))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.READY_TO_SUBMIT)

    def test_the_purpose_constant_matches_the_enum(self):
        self.assertEqual(NEST_FUNDS_PURPOSE, P.NEST_APPLICATION_FEE.value)

    def test_refused_with_no_fund_request(self):
        self.assertRefused()

    def test_refused_while_the_nest_request_is_not_yet_paid(self):
        fr = fund_request(self.tender, self.pm)
        self.assertRefused()  # PENDING
        fr.approve(self.md)
        self.assertRefused()  # APPROVED, not paid

    def test_refused_when_the_nest_request_was_declined(self):
        fund_request(self.tender, self.pm).decline(self.md, "Not now.")
        self.assertRefused()

    def test_a_paid_request_for_any_other_purpose_does_not_count(self):
        for purpose in (P.BID_SECURITY, P.OTHER, P.TENDER_DOCUMENT_FEE):
            with self.subTest(purpose=purpose):
                paid_fund_request(self, self.tender, purpose=purpose)
                self.assertRefused()

    def test_a_paid_nest_request_for_another_tender_does_not_count(self):
        paid_fund_request(self, self.new_tender(S.DRAFTING))
        self.assertRefused()

    def test_succeeds_with_a_paid_nest_application_fee(self):
        paid_fund_request(self, self.tender)
        self.tender.refresh_from_db()
        self.assertTrue(self.tender.has_nest_funds)
        self.tender.mark_submitted(self.pm)
        self.assertEqual(self.tender.status, S.SUBMITTED)

    def test_other_paid_purposes_alongside_a_paid_nest_fee_are_fine(self):
        paid_fund_request(self, self.tender, purpose=P.BID_SECURITY)
        paid_fund_request(self, self.tender)
        self.tender.mark_submitted(self.pm)
        self.assertEqual(self.tender.status, S.SUBMITTED)

    def test_only_the_pm_can_mark_submitted_even_when_funded(self):
        paid_fund_request(self, self.tender)
        for user in (self.finance, self.md, self.hops, self.pm2):
            with self.subTest(role=user.role, who=user.first_name), self.assertRaises(PermissionDenied):
                self.tender.mark_submitted(user)

    def test_finance_never_writes_to_tender(self):
        before = status_entries(self.tender).count()
        fr = fund_request(self.tender, self.pm)
        fr.approve(self.md)
        fr.mark_paid(self.finance, "REF")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.READY_TO_SUBMIT)  # paying does not submit
        self.assertEqual(status_entries(self.tender).count(), before)
        self.assertFalse(AuditEntry.objects.filter(
            model="tenders.Tender", object_id=str(self.tender.pk), actor=self.finance
        ).exists())
