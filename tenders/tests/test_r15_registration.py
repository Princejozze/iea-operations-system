"""
R15 — every tender needs MD registration approval. No item, costing or RFQ
work may begin before it. Declining sends the tender to WITHDRAWN.

The APPROVED/DECLINED happy paths need an Approval record (approvals app, not
built yet). Until it exists the gate must refuse — that is tested here.
"""

from django.core.exceptions import PermissionDenied
from django.test import TestCase

from tenders.models import TenderStatus as S, TransitionError

from .helpers import TenderFixtures, confirmed_fields, status_entries

UNREGISTERED = (S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL)


class RegistrationGateTests(TenderFixtures, TestCase):
    def test_pm_confirmation_lands_in_pending_registration_approval(self):
        tender = self.new_tender(S.PENDING_VERIFICATION)
        tender.confirm_extraction(self.pm, **confirmed_fields())
        self.assertEqual(tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_only_exec_can_decide_registration(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        for user in (self.pm, self.pm2, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role), self.assertRaises(PermissionDenied):
                tender.approve_registration(user)
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_exec_without_an_approval_record_is_refused(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        with self.assertRaises(TransitionError) as ctx:
            tender.approve_registration(self.md)
        self.assertIn("R15", str(ctx.exception))
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_registration_cannot_be_decided_from_other_states(self):
        for status in (S.EXTRACTING, S.PENDING_VERIFICATION, S.DRAFTING, S.COSTING):
            with self.subTest(status=status):
                tender = self.new_tender(status)
                with self.assertRaises(TransitionError):
                    tender.approve_registration(self.md)

    def test_no_costing_before_registration(self):
        for status in UNREGISTERED:
            with self.subTest(status=status):
                tender = self.new_tender(status)
                with self.assertRaises(TransitionError):
                    tender.start_costing(self.pm)
                tender.refresh_from_db()
                self.assertEqual(tender.status, status)

    def test_work_allowed_only_after_registration(self):
        for status in UNREGISTERED + (S.WITHDRAWN,):
            with self.subTest(status=status):
                self.assertFalse(self.new_tender(status).work_allowed)
        for status in (S.DRAFTING, S.COSTING):
            with self.subTest(status=status):
                self.assertTrue(self.new_tender(status).work_allowed)

    def test_registered_tender_can_start_costing(self):
        tender = self.new_tender(S.DRAFTING)
        tender.start_costing(self.pm)
        self.assertEqual(tender.status, S.COSTING)
        self.assertEqual(status_entries(tender).last().actor, self.pm)

    def test_md_can_decline_by_withdrawing_and_it_stays_dead(self):
        tender = self.new_tender(S.PENDING_REGISTRATION_APPROVAL, **confirmed_fields())
        tender.withdraw(self.md)
        self.assertEqual(tender.status, S.WITHDRAWN)
        for attempt in (
            lambda: tender.start_costing(self.pm),
            lambda: tender.approve_registration(self.md),
            lambda: tender.confirm_extraction(self.pm, **confirmed_fields()),
        ):
            with self.assertRaises(TransitionError):
                attempt()
