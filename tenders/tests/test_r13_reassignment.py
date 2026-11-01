"""
R13 — only EXEC reassigns a tender, and only to a user with role PM. The
change writes a TenderAssignment; both PMs are notified.
"""

from django.contrib.admin.sites import site
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase

from accounts.models import Role
from core.models import AuditAction, AuditEntry
from tenders.models import GuardedFieldError, Tender, TenderAssignment, TenderStatus as S

from .helpers import TenderFixtures, make_user


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
