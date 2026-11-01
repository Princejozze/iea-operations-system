"""
Approval (§7) and the gates that read it: R15 registration, R3 submission,
R9 decline. Every gate is tested in both directions: refused without the
approval, allowed with it.
"""

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from approvals.models import Approval, ApprovalDecision as D, ApprovalKind as K
from tenders.models import GuardedFieldError, TenderStatus as S, TransitionError
from tenders.tests.helpers import confirmed_fields

from .helpers import TenderFixtures, force_status, pending_executive_approval, sign, status_entries


class ApprovalModelTests(TenderFixtures, TestCase):
    def test_kinds_are_registration_and_submission_only(self):
        self.assertEqual(set(K.values), {"REGISTRATION", "SUBMISSION"})  # no ELIGIBILITY (§7)
        self.assertEqual(set(D.values), {"APPROVED", "DECLINED"})

    def test_only_exec_can_sign(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        for user in (self.pm, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role), self.assertRaises(PermissionDenied):
                sign(tender, K.REGISTRATION, D.APPROVED, user)
        self.assertFalse(Approval.objects.exists())

    def test_signature_is_typed_name_and_password_and_only_the_name_is_stored(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        approval = sign(tender, K.REGISTRATION, D.APPROVED, self.md, typed_name="  faraja   TEST ")
        self.assertEqual(approval.signature, "faraja TEST")
        self.assertEqual(approval.decided_by, self.md)
        self.assertIsNotNone(approval.decided_at)
        self.assertNotIn("password", {f.name for f in Approval._meta.get_fields()})

    def test_a_wrong_password_or_wrong_name_does_not_sign(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        for kwargs in ({"password": "wrong"}, {"password": ""}, {"typed_name": "Someone Else"}, {"typed_name": ""}):
            with self.subTest(**kwargs), self.assertRaises(PermissionDenied):
                sign(tender, K.REGISTRATION, D.APPROVED, self.md, **kwargs)
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.PENDING_REGISTRATION_APPROVAL)
        self.assertFalse(Approval.objects.exists())

    def test_an_approval_cannot_be_created_around_the_manager(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        with self.assertRaises(GuardedFieldError):
            Approval(tender=tender, kind=K.REGISTRATION, decision=D.APPROVED, decided_by=self.md,
                     signature="Faraja Test").save()
        with self.assertRaises(GuardedFieldError):
            Approval.objects.create(tender=tender, kind=K.REGISTRATION, decision=D.APPROVED,
                                    decided_by=self.md, signature="Faraja Test")
        self.assertFalse(Approval.objects.exists())

    def test_a_signed_approval_cannot_be_edited_or_deleted(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        approval = sign(tender, K.REGISTRATION, D.APPROVED, self.md)
        approval.comments = "changed my mind"
        with self.assertRaises(ValidationError):
            approval.save()
        with self.assertRaises(PermissionDenied):
            approval.delete()
        with self.assertRaises(PermissionDenied):
            Approval.objects.all().delete()
        self.assertEqual(Approval.objects.count(), 1)

    def test_an_approval_can_only_be_signed_in_the_state_it_decides(self):
        wrong = {
            K.REGISTRATION: (S.DRAFTING, S.COSTING, S.PENDING_EXECUTIVE_APPROVAL, S.WITHDRAWN),
            K.SUBMISSION: (S.PENDING_REGISTRATION_APPROVAL, S.DRAFTING, S.COSTING, S.READY_TO_SUBMIT),
        }
        for kind, statuses in wrong.items():
            for status in statuses:
                with self.subTest(kind=kind, status=status):
                    tender = self.new_tender(status)
                    with self.assertRaises(ValidationError):
                        sign(tender, kind, D.APPROVED, self.md)
        self.assertFalse(Approval.objects.exists())

    def test_a_decline_must_give_its_reason(self):
        for kind, status in ((K.REGISTRATION, S.PENDING_REGISTRATION_APPROVAL),
                             (K.SUBMISSION, S.PENDING_EXECUTIVE_APPROVAL)):
            for comments in ("", "   "):
                with self.subTest(kind=kind, comments=comments):
                    tender = self.new_tender(status, **(confirmed_fields() if kind == K.REGISTRATION else {}))
                    with self.assertRaises(ValidationError):
                        sign(tender, kind, D.DECLINED, self.md, comments=comments)
                    tender.refresh_from_db()
                    self.assertEqual(tender.status, status)
        self.assertFalse(Approval.objects.exists())


class R15RegistrationTests(TenderFixtures, TestCase):
    """R15 success paths, now that Approval exists. (Other refusals: tenders/tests/test_r15_registration.py.)"""

    def setUp(self):
        self.tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())

    def test_md_approval_opens_the_tender_for_work(self):
        with self.captureOnCommitCallbacks(execute=True):
            sign(self.tender, K.REGISTRATION, D.APPROVED, self.md, comments="Pursue.")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.DRAFTING)
        self.assertTrue(self.tender.work_allowed)
        self.assertEqual(status_entries(self.tender).last().actor, self.md)
        self.tender.start_costing(self.pm)  # work can now begin
        self.assertEqual(self.tender.status, S.COSTING)

    def test_md_decline_withdraws_the_tender_and_it_stays_dead(self):
        sign(self.tender, K.REGISTRATION, D.DECLINED, self.md, comments="Outside our categories.")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.WITHDRAWN)
        with self.assertRaises(TransitionError):
            self.tender.start_costing(self.pm)

    def test_the_pm_is_told_of_the_decision(self):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            sign(self.tender, K.REGISTRATION, D.DECLINED, self.md, comments="Outside our categories.")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.pm.email])
        self.assertIn("Outside our categories.", mail.outbox[0].body)

    def test_without_an_approval_the_gate_still_refuses(self):
        with self.assertRaises(TransitionError) as ctx:
            self.tender.approve_registration(self.md)
        self.assertIn("R15", str(ctx.exception))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_a_submission_approval_does_not_open_registration(self):
        # Wrong kind: even a genuine MD SUBMISSION approval cannot satisfy R15.
        other = pending_executive_approval(self)
        sign(other, K.SUBMISSION, D.APPROVED, self.md)
        with self.assertRaises(TransitionError):
            self.tender.approve_registration(self.md)


class R3SubmissionTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = pending_executive_approval(self)

    def test_md_approval_makes_the_tender_ready_to_submit(self):
        sign(self.tender, K.SUBMISSION, D.APPROVED, self.md, comments="Good to go.")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.READY_TO_SUBMIT)
        self.assertIsNotNone(self.tender.exchange_rate_locked_at)  # the lock survives approval (R4)

    def test_there_is_no_route_to_ready_to_submit_without_the_approval(self):
        with self.assertRaises(TransitionError) as ctx:
            self.tender.approve_submission(self.md)
        self.assertIn("R3", str(ctx.exception))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_EXECUTIVE_APPROVAL)

    def test_only_exec_can_run_the_gate(self):
        for user in (self.pm, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role), self.assertRaises(PermissionDenied):
                self.tender.approve_submission(user)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_EXECUTIVE_APPROVAL)

    def test_a_registration_approval_does_not_satisfy_r3(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        sign(tender, K.REGISTRATION, D.APPROVED, self.md)  # → DRAFTING, holds a REGISTRATION approval
        tender = pending_executive_approval(self, tender)
        with self.assertRaises(TransitionError):
            tender.approve_submission(self.md)

    def test_a_declined_approval_does_not_make_it_ready(self):
        sign(self.tender, K.SUBMISSION, D.DECLINED, self.md, comments="Margins too thin.")
        self.tender.refresh_from_db()
        self.assertNotEqual(self.tender.status, S.READY_TO_SUBMIT)


class R9DeclinedSubmissionTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = pending_executive_approval(self)
        self.assertIsNotNone(self.tender.exchange_rate_locked_at)

    def decline(self, reason="Margins too thin on lot 2."):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            return sign(self.tender, K.SUBMISSION, D.DECLINED, self.md, comments=reason)

    def test_decline_returns_to_costing_and_clears_the_rate_lock(self):
        self.decline()
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.COSTING)
        self.assertIsNone(self.tender.exchange_rate_locked_at)  # R4

    def test_decline_notifies_the_pm_with_the_reason(self):
        self.decline("Margins too thin on lot 2.")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.pm.email])
        self.assertIn("returned to COSTING", mail.outbox[0].body)
        self.assertIn("Margins too thin on lot 2.", mail.outbox[0].body)
        self.assertIn(self.tender.reference, mail.outbox[0].subject)

    def test_decline_records_the_reason(self):
        approval = self.decline("Margins too thin on lot 2.")
        self.assertEqual(approval.comments, "Margins too thin on lot 2.")
        change = status_entries(self.tender).last()
        self.assertEqual(change.changes["status"], [S.PENDING_EXECUTIVE_APPROVAL, S.COSTING])
        self.assertEqual(change.actor, self.md)

    def test_decline_is_not_a_dead_end(self):
        self.decline()
        self.tender.refresh_from_db()
        self.assertTrue(self.tender.work_allowed)  # the PM keeps working
        self.assertNotIn(self.tender.status, (S.WITHDRAWN, S.READY_TO_SUBMIT))

    def test_an_old_decline_is_not_reused_next_round(self):
        self.decline()
        # PM fixes the sheet, audit passes, and the tender is back awaiting the MD.
        force_status(self.tender, S.PENDING_EXECUTIVE_APPROVAL, exchange_rate_locked_at=timezone.now())
        with self.assertRaises(TransitionError):
            self.tender.approve_submission(self.md)  # last round's DECLINED must not count
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_EXECUTIVE_APPROVAL)
        sign(self.tender, K.SUBMISSION, D.APPROVED, self.md)  # a fresh decision does
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.READY_TO_SUBMIT)
