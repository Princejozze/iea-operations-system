"""
R19 — status changes only through transition methods. Each is named for its
event, takes the acting user, checks its rules, writes an AuditEntry, and is
the only writer of status. (R1, R2, R3, R4, R9, R16, R17 and R20 are checked
where their transitions are exercised.)
"""

import datetime
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import Role
from core.models import AuditAction, AuditBypassError, AuditEntry
from tenders.models import (
    AwardStatus,
    GuardedFieldError,
    Tender,
    TenderAssignment,
    TenderOutcome,
    TenderStatus as S,
    TransitionError,
    WITHDRAWABLE_STATUSES,
)

from .helpers import TenderFixtures, confirmed_fields, force_status, status_entries


class TenderCreationTests(TenderFixtures, TestCase):
    def test_upload_creates_tender_with_reference_and_status_extracting(self):
        tender = Tender.objects.create_from_upload(uploaded_by=self.pm)
        year = timezone.localdate().year
        self.assertEqual(tender.reference, f"IEA-{year}-0001")  # R20, from core
        self.assertEqual(tender.status, S.EXTRACTING)
        self.assertEqual(tender.created_by, self.pm)
        self.assertEqual(tender.project_manager, self.pm)
        second = Tender.objects.create_from_upload(uploaded_by=self.pm2)
        self.assertEqual(second.reference, f"IEA-{year}-0002")

    def test_upload_is_audited_and_records_first_assignment(self):
        tender = Tender.objects.create_from_upload(uploaded_by=self.pm)
        create = AuditEntry.objects.get(model="tenders.Tender", object_id=str(tender.pk))
        self.assertEqual(create.action, AuditAction.CREATE)
        self.assertEqual(create.actor, self.pm)
        self.assertEqual(create.changes["status"], [None, S.EXTRACTING])
        assignment = TenderAssignment.objects.get(tender=tender)
        self.assertIsNone(assignment.from_pm)
        self.assertEqual(assignment.to_pm, self.pm)

    def test_only_a_pm_can_create_by_upload(self):
        for user in (self.md, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role), self.assertRaises(PermissionDenied):
                Tender.objects.create_from_upload(uploaded_by=user)
        self.assertFalse(Tender.objects.exists())

    def test_no_other_creation_path(self):
        with self.assertRaises(GuardedFieldError):
            Tender(created_by=self.pm, project_manager=self.pm).save()
        with self.assertRaises(GuardedFieldError):
            Tender.objects.create(created_by=self.pm, project_manager=self.pm, title="Blank form")
        self.assertFalse(Tender.objects.exists())


class StatusIsWrittenOnlyByTransitionsTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender()

    def test_assigning_status_and_saving_is_refused(self):
        self.tender.status = S.WON
        with self.assertRaises(GuardedFieldError):
            self.tender.save()
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.EXTRACTING)

    def test_other_system_fields_are_guarded_too(self):
        for field, value in (
            ("reference", "IEA-2026-9999"),
            ("previous_status", S.DRAFTING),
            ("exchange_rate_locked_at", timezone.now()),
            ("project_manager", self.pm2),
            ("created_by", self.pm2),
        ):
            with self.subTest(field=field):
                tender = Tender.objects.get(pk=self.tender.pk)
                setattr(tender, field, value)
                with self.assertRaises(GuardedFieldError):
                    tender.save()

    def test_ordinary_fields_save_normally(self):
        self.tender.title = "Supply of autoclaves"
        self.tender.save(actor=self.pm)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.title, "Supply of autoclaves")

    def test_queryset_update_of_status_is_refused(self):
        with self.assertRaises(AuditBypassError):
            Tender.objects.filter(pk=self.tender.pk).update(status=S.SUBMITTED)

    def test_database_rejects_an_unknown_status(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE tenders_tender SET status = 'QUOTED' WHERE id = %s", [self.tender.pk])

    def test_database_rejects_rate_lock_before_audit(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "UPDATE tenders_tender SET exchange_rate_locked_at = now() WHERE id = %s", [self.tender.pk]
            )

    def test_status_max_length_fits_longest_value(self):
        field = Tender._meta.get_field("status")
        self.assertEqual(field.max_length, 32)
        self.assertGreaterEqual(field.max_length, max(len(v) for v in S.values))


class TransitionAuditTests(TenderFixtures, TestCase):
    def test_each_transition_writes_one_status_change_entry_with_actor(self):
        tender = self.new_tender()
        tender.finish_extraction()
        tender.confirm_extraction(self.pm, **confirmed_fields())
        tender.withdraw(self.md)
        entries = list(status_entries(tender))
        self.assertEqual(
            [e.changes["status"] for e in entries],
            [
                [S.EXTRACTING, S.PENDING_VERIFICATION],
                [S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL],
                [S.PENDING_REGISTRATION_APPROVAL, S.WITHDRAWN],
            ],
        )
        self.assertEqual([e.actor for e in entries], [None, self.pm, self.md])

    def test_refused_transition_changes_nothing_and_writes_nothing(self):
        tender = self.new_tender()
        before = AuditEntry.objects.count()
        with self.assertRaises(TransitionError):
            tender.start_costing(self.pm)
        self.assertEqual(AuditEntry.objects.count(), before)
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.EXTRACTING)

    def test_transition_checks_database_status_not_a_stale_instance(self):
        tender = self.new_tender()
        stale = Tender.objects.get(pk=tender.pk)
        tender.finish_extraction()
        with self.assertRaises(TransitionError):
            stale.finish_extraction()  # stale copy still says EXTRACTING
        self.assertEqual(status_entries(tender).count(), 1)


class ConfirmExtractionTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.PENDING_VERIFICATION)

    def test_pm_confirmation_copies_fields_and_moves_to_registration(self):
        self.tender.confirm_extraction(self.pm, **confirmed_fields())
        self.assertEqual(self.tender.status, S.PENDING_REGISTRATION_APPROVAL)
        self.assertEqual(self.tender.official_reference, "MNH/2026/G/017")
        entry = status_entries(self.tender).last()
        self.assertEqual(entry.changes["title"], ["", "Supply of laboratory centrifuges"])

    def test_missing_required_fields_refuse_and_roll_back(self):
        fields = confirmed_fields()
        del fields["official_reference"]
        with self.assertRaises(TransitionError) as ctx:
            self.tender.confirm_extraction(self.pm, **fields)
        self.assertIn("official_reference", ctx.exception.message_dict)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_VERIFICATION)
        self.assertEqual(self.tender.title, "")  # nothing partially applied

    def test_invalid_values_refuse(self):
        fields = confirmed_fields()
        fields["clarification_deadline"] = fields["submission_deadline"] + datetime.timedelta(days=1)
        with self.assertRaises(ValidationError):
            self.tender.confirm_extraction(self.pm, **fields)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_VERIFICATION)

    def test_only_this_tenders_pm_confirms(self):
        for user in (self.pm2, self.md, self.hops, self.auditor):
            with self.subTest(user=user.email), self.assertRaises(PermissionDenied):
                self.tender.confirm_extraction(user, **confirmed_fields())

    def test_only_confirmable_fields_accepted(self):
        with self.assertRaises(TypeError):
            self.tender.confirm_extraction(self.pm, status=S.WON, **confirmed_fields())

    def test_not_from_extracting(self):
        tender = self.new_tender()
        with self.assertRaises(TransitionError):
            tender.confirm_extraction(self.pm, **confirmed_fields())


class GatesFailClosedTests(TenderFixtures, TestCase):
    """Gates whose evidence lives in apps not yet built must refuse, not pass."""

    def test_r2_submit_for_audit_refused_without_item_sheet(self):
        tender = self.new_tender(S.COSTING)
        with self.assertRaises(TransitionError) as ctx:
            tender.submit_for_audit(self.pm)
        self.assertIn("R2", str(ctx.exception))
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.COSTING)
        self.assertIsNone(tender.exchange_rate_locked_at)  # R4 not stamped

    def test_record_audit_result_refused_without_a_qc_audit(self):
        tender = self.new_tender(S.PENDING_AUDIT, exchange_rate_locked_at=timezone.now())
        with self.assertRaises(TransitionError):
            tender.record_audit_result(self.auditor, compliant=True)
        self.assertEqual(tender.status, S.PENDING_AUDIT)

    def test_r3_approve_submission_refused_without_approval(self):
        tender = self.new_tender(S.PENDING_EXECUTIVE_APPROVAL, exchange_rate_locked_at=timezone.now())
        with self.assertRaises(TransitionError) as ctx:
            tender.approve_submission(self.md)
        self.assertIn("R3", str(ctx.exception))

    def test_r17_mark_submitted_refused_without_paid_fund_request(self):
        tender = self.new_tender(S.READY_TO_SUBMIT, exchange_rate_locked_at=timezone.now())
        self.assertFalse(tender.has_nest_funds)
        with self.assertRaises(TransitionError) as ctx:
            tender.mark_submitted(self.pm)
        self.assertIn("R17", str(ctx.exception))

    def test_r16_raise_and_resolve_refused_without_eligibility_review(self):
        tender = self.new_tender(S.DRAFTING)
        with self.assertRaises(TransitionError):
            tender.raise_eligibility(self.pm)
        force_status(tender, S.PENDING_ELIGIBILITY_DECISION, previous_status=S.DRAFTING)
        with self.assertRaises(TransitionError):
            tender.resolve_eligibility(self.md)


class RoleChecksTests(TenderFixtures, TestCase):
    def test_r1_pm_cannot_audit_own_tender_and_only_auditors_audit(self):
        tender = self.new_tender(S.PENDING_AUDIT, exchange_rate_locked_at=timezone.now())
        self.assertFalse(self.pm.can_audit(tender))
        self.assertTrue(self.auditor.can_audit(tender))
        with self.assertRaises(ValidationError):
            self.pm.assert_can_audit(tender)
        for user in (self.pm, self.pm2, self.md):
            with self.subTest(user=user.email), self.assertRaises(PermissionDenied):
                tender.record_audit_result(user, compliant=True)

    def test_r16_only_exec_resolves_eligibility(self):
        tender = self.new_tender(S.PENDING_ELIGIBILITY_DECISION, previous_status=S.COSTING)
        for user in (self.pm, self.hops, self.admin_user):
            with self.subTest(user=user.email), self.assertRaises(PermissionDenied):
                tender.resolve_eligibility(user)

    def test_inactive_user_cannot_transition(self):
        tender = self.new_tender(S.DRAFTING)
        self.pm.is_active = False
        with self.assertRaises(PermissionDenied):
            tender.start_costing(self.pm)


class WithdrawTests(TenderFixtures, TestCase):
    def test_exec_can_withdraw_from_every_pre_submission_state(self):
        for status in WITHDRAWABLE_STATUSES:
            with self.subTest(status=status):
                extra = {}
                if status == S.PENDING_ELIGIBILITY_DECISION:
                    extra["previous_status"] = S.DRAFTING
                if status in (S.PENDING_AUDIT, S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT):
                    extra["exchange_rate_locked_at"] = timezone.now()
                tender = self.new_tender(status, **extra)
                tender.withdraw(self.md)
                self.assertEqual(tender.status, S.WITHDRAWN)
                self.assertEqual(tender.previous_status, "")

    def test_cannot_withdraw_after_submission(self):
        for status in (S.SUBMITTED, S.WON, S.LOST, S.WITHDRAWN):
            with self.subTest(status=status):
                tender = self.new_tender(status)
                with self.assertRaises(TransitionError):
                    tender.withdraw(self.md)

    def test_only_exec_withdraws(self):
        tender = self.new_tender(S.DRAFTING)
        for user in (self.pm, self.hops, self.finance, self.admin_user):
            with self.subTest(user=user.email), self.assertRaises(PermissionDenied):
                tender.withdraw(user)


class RecordOutcomeTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.SUBMITTED)

    def test_awarded_drives_won(self):
        self.tender.record_outcome(
            self.pm, award_status=AwardStatus.AWARDED, bid_opening_position="LOWEST",
            contract_sum=Decimal("125000000.00"), contract_currency="TZS",
        )
        self.assertEqual(self.tender.status, S.WON)
        outcome = TenderOutcome.objects.get(tender=self.tender)
        self.assertEqual(outcome.contract_sum, Decimal("125000000.00"))
        self.assertEqual(outcome.recorded_by, self.pm)

    def test_not_awarded_and_cancelled_drive_lost(self):
        for award in (AwardStatus.NOT_AWARDED, AwardStatus.CANCELLED):
            with self.subTest(award=award):
                tender = self.new_tender(S.SUBMITTED)
                tender.record_outcome(self.md, award_status=award)
                self.assertEqual(tender.status, S.LOST)

    def test_pending_leaves_submitted_and_writes_no_status_change(self):
        before = status_entries(self.tender).count()
        self.tender.record_outcome(self.pm, award_status=AwardStatus.PENDING, bid_opening_position="SECOND_LOWEST")
        self.assertEqual(self.tender.status, S.SUBMITTED)
        self.assertEqual(status_entries(self.tender).count(), before)
        self.assertEqual(self.tender.outcome.bid_opening_position, "SECOND_LOWEST")

    def test_only_this_pm_or_exec(self):
        for user in (self.pm2, self.finance, self.auditor):
            with self.subTest(user=user.email), self.assertRaises(PermissionDenied):
                self.tender.record_outcome(user, award_status=AwardStatus.AWARDED)

    def test_contract_sum_needs_currency_and_failure_rolls_back(self):
        with self.assertRaises(ValidationError):
            self.tender.record_outcome(self.pm, award_status=AwardStatus.AWARDED, contract_sum=Decimal("1"))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.SUBMITTED)
        self.assertFalse(TenderOutcome.objects.exists())

    def test_terminal_states_cannot_be_re_recorded(self):
        self.tender.record_outcome(self.pm, award_status=AwardStatus.NOT_AWARDED)
        with self.assertRaises(TransitionError):
            self.tender.record_outcome(self.pm, award_status=AwardStatus.AWARDED)
