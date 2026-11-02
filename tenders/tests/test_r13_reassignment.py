"""
R13 — only EXEC reassigns a tender: to any active user with role PM, at any
status. The change writes a TenderAssignment; both PMs are notified.
"""

from django.contrib.admin.sites import site
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role
from core.models import AuditAction, AuditEntry
from tenders.models import (
    RATE_LOCKED_STATUSES,
    AwardStatus,
    GuardedFieldError,
    Tender,
    TenderAssignment,
    TenderStatus as S,
    TransitionError,
)

from .helpers import TenderFixtures, add_item, make_user


class ReassignmentTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.DRAFTING)
        mail.outbox.clear()

    def test_exec_reassigns_writes_history_audit_and_notifies_both_pms(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.tender.reassign(self.md, self.pm2, reason="Gift on leave")

        self.assertEqual(self.tender.project_manager, self.pm2)

        row = TenderAssignment.objects.filter(tender=self.tender).last()
        self.assertEqual((row.from_pm, row.to_pm, row.assigned_by), (self.pm, self.pm2, self.md))
        self.assertEqual(row.reason, "Gift on leave")

        entry = AuditEntry.objects.filter(
            model="tenders.Tender", object_id=str(self.tender.pk), action=AuditAction.UPDATE
        ).last()
        self.assertEqual(entry.actor, self.md)
        self.assertEqual(entry.changes, {"project_manager_id": [self.pm.pk, self.pm2.pk]})

        self.assertEqual(len(mail.outbox), 1)
        self.assertCountEqual(mail.outbox[0].to, [self.pm.email, self.pm2.email])
        self.assertIn(self.tender.reference, mail.outbox[0].subject)

    def test_nobody_but_exec_can_reassign(self):
        superuser_hops = make_user(Role.HEAD_OF_OPERATIONS, "dinna", is_staff=True, is_superuser=True)
        for user in (self.pm, self.pm2, self.hops, self.auditor, self.finance, self.admin_user, superuser_hops):
            with self.subTest(role=user.role, superuser=user.is_superuser):
                with self.captureOnCommitCallbacks(execute=True), self.assertRaises(PermissionDenied):
                    self.tender.reassign(user, self.pm2)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.project_manager, self.pm)
        self.assertEqual(TenderAssignment.objects.filter(tender=self.tender).count(), 1)  # upload row only
        self.assertEqual(mail.outbox, [])

    def test_only_to_an_active_pm(self):
        inactive_pm = make_user(Role.PROJECT_MANAGER, "inactive", is_active=False)
        for target in (self.md, self.hops, self.auditor, self.finance, self.admin_user, inactive_pm):
            with self.subTest(role=target.role, active=target.is_active):
                with self.assertRaises(ValidationError):
                    self.tender.reassign(self.md, target)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.project_manager, self.pm)

    def test_reassigning_to_the_current_pm_is_refused(self):
        with self.assertRaises(ValidationError):
            self.tender.reassign(self.md, self.pm)
        self.assertEqual(TenderAssignment.objects.filter(tender=self.tender).count(), 1)

    def test_project_manager_cannot_be_changed_by_a_direct_save(self):
        self.tender.project_manager = self.pm2
        with self.assertRaises(GuardedFieldError):
            self.tender.save()
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.project_manager, self.pm)

    def test_admin_form_does_not_offer_project_manager(self):
        request = RequestFactory().get("/")
        request.user = self.admin_user
        form_class = site._registry[Tender].get_form(request, self.tender)
        self.assertNotIn("project_manager", form_class.base_fields)
        self.assertNotIn("status", form_class.base_fields)

    def test_after_reassignment_only_the_new_pm_can_act(self):
        add_item(self.tender)  # so only the PM check, not the item check, decides
        self.tender.reassign(self.md, self.pm2)
        with self.assertRaises(PermissionDenied):
            self.tender.start_costing(self.pm)
        self.tender.start_costing(self.pm2)
        self.assertEqual(self.tender.status, S.COSTING)

    def test_no_notification_if_reassignment_fails(self):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValidationError):
                self.tender.reassign(self.md, self.auditor)
        self.assertEqual(mail.outbox, [])


class ReassignmentAtEveryStatusTests(TenderFixtures, TestCase):
    """
    R13: any active PM, at ANY status. There is no status restriction: outcome capture,
    award recording and post-award work still need an owner after submission.
    """

    ALL = tuple(S.values)

    def tender_in(self, status):
        """A tender in ``status`` (test-only shortcut, see force_status), held by self.pm."""
        if status == S.EXTRACTING:
            return self.new_tender()  # where an upload starts
        extra = {}
        if status == S.PENDING_ELIGIBILITY_DECISION:
            extra["previous_status"] = S.COSTING
        if status in RATE_LOCKED_STATUSES:
            extra["exchange_rate_locked_at"] = timezone.now()
        return self.new_tender(status, **extra)

    def holds_pm_rights(self, tender, user):
        """Does ``user`` pass this tender's PM-ownership check?

        Tender._transition checks "this tender's PM" BEFORE the status, so a PM-only
        transition answers the question from any status without changing anything: a
        non-owner is refused with PermissionDenied, the owner gets past it and is then
        refused on status (or, in DRAFTING with no items, on the item check).
        """
        try:
            with transaction.atomic():
                tender.start_costing(user)
        except PermissionDenied:
            return False
        except TransitionError:
            return True
        raise AssertionError(f"start_costing unexpectedly succeeded from {tender.status}")

    def test_every_status_is_covered_including_after_submission(self):
        self.assertTrue({S.SUBMITTED, S.WON, S.LOST, S.WITHDRAWN} <= set(self.ALL))
        self.assertEqual(len(self.ALL), 13)  # a new status must be added to these tests deliberately

    def test_reassignment_succeeds_from_every_status(self):
        for status in self.ALL:
            with self.subTest(status=status):
                tender = self.tender_in(status)
                mail.outbox.clear()
                with self.captureOnCommitCallbacks(execute=True):
                    tender.reassign(self.md, self.pm2, reason=f"Cover while in {status}")

                tender.refresh_from_db()
                self.assertEqual(tender.project_manager, self.pm2)
                self.assertEqual(tender.status, status)  # reassignment never moves the status
                row = TenderAssignment.objects.filter(tender=tender).last()
                self.assertEqual((row.from_pm, row.to_pm, row.assigned_by), (self.pm, self.pm2, self.md))
                entry = AuditEntry.objects.filter(
                    model="tenders.Tender", object_id=str(tender.pk), action=AuditAction.UPDATE
                ).last()
                self.assertEqual((entry.actor, entry.changes), (self.md, {"project_manager_id": [self.pm.pk, self.pm2.pk]}))
                self.assertEqual(len(mail.outbox), 1)
                self.assertCountEqual(mail.outbox[0].to, [self.pm.email, self.pm2.email])

    def test_access_moves_to_the_new_pm_and_leaves_the_old_one_at_every_status(self):
        for status in self.ALL:
            with self.subTest(status=status):
                tender = self.tender_in(status)
                self.assertTrue(self.holds_pm_rights(tender, self.pm))
                self.assertFalse(self.holds_pm_rights(tender, self.pm2))

                tender.reassign(self.md, self.pm2)

                # Model layer: every "this tender's PM" rule now answers to the new PM.
                self.assertTrue(self.holds_pm_rights(tender, self.pm2))
                self.assertFalse(self.holds_pm_rights(tender, self.pm))
                self.assertEqual(Tender.objects.get(pk=tender.pk).status, status)  # the probes changed nothing

                # Screens: the PM's own view of the tender's QC audit follows the owner, at any status.
                url = reverse("audits:detail", kwargs={"tender_pk": tender.pk})
                self.assertEqual(self.page(self.pm2, url), 200)
                self.assertEqual(self.page(self.pm, url), 403)

    def test_the_new_pm_verifies_a_tender_awaiting_verification(self):
        tender = self.tender_in(S.PENDING_VERIFICATION)
        tender.reassign(self.md, self.pm2)
        url = reverse("tenders:verify", kwargs={"pk": tender.pk})
        self.assertEqual(self.page(self.pm2, url), 200)
        self.assertEqual(self.page(self.pm, url), 403)

    def test_the_new_pm_records_the_outcome_after_submission(self):
        tender = self.tender_in(S.SUBMITTED)
        tender.reassign(self.md, self.pm2)
        with self.assertRaises(PermissionDenied):
            tender.record_outcome(self.pm, award_status=AwardStatus.AWARDED)
        tender.record_outcome(self.pm2, award_status=AwardStatus.AWARDED, bid_opening_position="LOWEST")
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.WON)
        self.assertEqual(tender.outcome.recorded_by, self.pm2)

    def test_the_new_pm_marks_a_ready_tender_submitted(self):
        tender = self.tender_in(S.READY_TO_SUBMIT)
        tender.reassign(self.md, self.pm2)
        with self.assertRaises(PermissionDenied):
            tender.mark_submitted(self.pm)
        with self.assertRaises(TransitionError) as ctx:  # past the owner check; R17 still applies
            tender.mark_submitted(self.pm2)
        self.assertEqual(ctx.exception.code, "r17")

    def test_a_won_or_lost_tender_can_be_reassigned_more_than_once(self):
        for status in (S.WON, S.LOST):
            with self.subTest(status=status):
                tender = self.tender_in(status)
                tender.reassign(self.md, self.pm2)
                tender.reassign(self.md, self.pm)
                self.assertEqual(tender.project_manager, self.pm)
                self.assertEqual(
                    list(TenderAssignment.objects.filter(tender=tender).values_list("to_pm", flat=True)),
                    [self.pm.pk, self.pm2.pk, self.pm.pk],  # upload, then the two moves
                )

    def test_a_non_pm_assignee_is_still_refused_at_every_status(self):
        clearing_agent = make_user(Role.CLEARING_AGENT, "dinna")
        inactive_pm = make_user(Role.PROJECT_MANAGER, "inactive.pm", is_active=False)
        targets = (self.md, self.hops, self.auditor, self.finance, self.admin_user, clearing_agent, inactive_pm)
        for status in self.ALL:
            tender = self.tender_in(status)
            for target in targets:
                with self.subTest(status=status, role=target.role, active=target.is_active):
                    with self.captureOnCommitCallbacks(execute=True), self.assertRaises(ValidationError) as ctx:
                        tender.reassign(self.md, target)
                    # Refused for the reason R13 gives, not for some other reason at this status.
                    self.assertIn("active Project Manager", " ".join(ctx.exception.messages))
            tender.refresh_from_db()
            self.assertEqual(tender.project_manager, self.pm)
            self.assertEqual(TenderAssignment.objects.filter(tender=tender).count(), 1)  # the upload row only

    def test_only_exec_reassigns_at_every_status(self):
        for status in self.ALL:
            tender = self.tender_in(status)
            for user in (self.pm, self.pm2, self.hops, self.admin_user):
                with self.subTest(status=status, role=user.role), self.assertRaises(PermissionDenied):
                    tender.reassign(user, self.pm2)

    def page(self, user, url):
        self.client.force_login(user)
        return self.client.get(url).status_code
