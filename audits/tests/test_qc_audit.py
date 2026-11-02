"""
QCAudit and QCAuditLine on PostgreSQL: R1, R2 (entry), R9, R9a, variance on lines.

The tender reaches PENDING_AUDIT the real way: a costing tender with fully
complete items, then Tender.submit_for_audit (so R2 and the R4 rate lock run).
"""

from django.contrib import admin
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase

from accounts.models import Role
from audits.models import QCAudit, QCAuditLine, Verdict
from core.models import AuditAction, AuditEntry
from items.tests.helpers import ItemFixtures, make_user
from tenders.models import GuardedFieldError, TenderStatus as S, TransitionError
from tenders.tests.helpers import force_status


class AuditFixtures(ItemFixtures):
    SPEC = "Platform scale, capacity 656kg"

    def pending_audit_tender(self, *specs):
        tender = self.costing()
        items = [self.complete_item(tender, specification=s) for s in (specs or (self.SPEC,))]
        tender.submit_for_audit(self.pm)
        self.assertEqual(tender.status, S.PENDING_AUDIT)
        self.assertIsNotNone(tender.exchange_rate_locked_at)
        return tender, items

    def open_audit(self, *specs):
        tender, items = self.pending_audit_tender(*specs)
        return QCAudit.objects.start(tender, self.auditor), tender, items

    def decide(self, audit, verdict, comments="", offered=None):
        for line in audit.lines.all():
            line.verdict = verdict
            line.comments = comments
            if offered is not None:
                line.specification_offered = offered
            line.save()


class StartTests(AuditFixtures, TestCase):
    def test_start_opens_the_audit_with_one_prefilled_undecided_line_per_item(self):
        audit, tender, items = self.open_audit("Scale 656kg", "Autoclave 100 l")
        self.assertEqual(audit.tender, tender)
        self.assertEqual(audit.auditor, self.auditor)
        self.assertEqual(audit.verdict, "")
        self.assertFalse(audit.is_complete)
        lines = {ln.item_id: ln for ln in audit.lines.all()}
        self.assertEqual(set(lines), {i.pk for i in items})
        for item in items:
            self.assertEqual(lines[item.pk].specification_required, item.specification)
            self.assertEqual(lines[item.pk].verdict, "")
        self.assertEqual(tender.qc_audits.get(), audit)  # the reverse accessor tenders reads

    def test_specification_offered_is_prefilled_from_the_selected_quote(self):
        tender = self.costing()
        quoted = self.complete_item(tender, specification="Scale, 656kg")
        unquoted = self.complete_item(tender, specification="Autoclave, 100 l")
        self.quote(
            quoted, specification_offered="Capacity 660 kg, stainless platform",
            make_brand="Eppendorf", model_part_number="5424 R", country_of_origin="Germany",
        ).select(self.pm)
        tender.submit_for_audit(self.pm)
        audit = QCAudit.objects.start(tender, self.auditor)
        self.assertEqual(audit.lines.get(item=quoted).specification_offered, "Capacity 660 kg, stainless platform")
        self.assertEqual(audit.lines.get(item=unquoted).specification_offered, "")  # no quote selected

    def test_creation_is_audited_with_the_auditor_as_actor(self):
        audit, *_ = self.open_audit()
        entry = AuditEntry.objects.get(model="audits.QCAudit", object_id=str(audit.pk), action=AuditAction.CREATE)
        self.assertEqual(entry.actor, self.auditor)
        line = audit.lines.get()
        self.assertTrue(
            AuditEntry.objects.filter(model="audits.QCAuditLine", object_id=str(line.pk), actor=self.auditor).exists()
        )


class R1Tests(AuditFixtures, TestCase):
    def test_a_pm_cannot_audit(self):
        tender, _ = self.pending_audit_tender()
        with self.assertRaises(ValidationError) as caught:
            QCAudit.objects.start(tender, self.pm)
        self.assertEqual(caught.exception.code, "r1")
        self.assertEqual(QCAudit.objects.count(), 0)

    def test_the_foreign_key_is_what_is_checked_not_the_role_label(self):
        # An auditor who is ALSO this tender's project_manager (here: the PM's role
        # was changed to AUDIT) is refused on the real foreign key, with R1's message.
        tender, _ = self.pending_audit_tender()
        pm = type(self.pm).objects.get(pk=self.pm.pk)
        pm.role = Role.AUDITOR
        pm.save()
        self.assertEqual(tender.project_manager_id, pm.pk)
        with self.assertRaisesMessage(ValidationError, "A PM cannot audit their own tender (R1)."):
            QCAudit.objects.start(tender, pm)

    def test_another_auditor_may_audit(self):
        tender, _ = self.pending_audit_tender()
        other = make_user(Role.AUDITOR, "anna.m")
        self.assertEqual(QCAudit.objects.start(tender, other).auditor, other)

    def test_inactive_and_non_auditor_roles_are_refused(self):
        tender, _ = self.pending_audit_tender()
        for user in (self.md, self.hops, self.finance, self.admin_user):
            with self.subTest(role=user.role), self.assertRaises(ValidationError):
                QCAudit.objects.start(tender, user)

    def test_save_enforces_it_too_not_just_clean(self):
        tender, _ = self.pending_audit_tender()
        with self.assertRaises(ValidationError):
            QCAudit(tender=tender, auditor=self.pm).save()

    def test_reassigning_the_tender_later_does_not_invalidate_a_completed_audit(self):
        audit, tender, _ = self.open_audit()
        self.decide(audit, Verdict.POSITIVE)
        audit.complete(self.auditor)
        tender.reassign(self.md, self.pm2)
        audit.refresh_from_db()
        self.assertEqual(audit.verdict, Verdict.POSITIVE)


class EntryGateTests(AuditFixtures, TestCase):
    def test_no_audit_unless_the_tender_is_pending_audit(self):
        tender = self.costing()
        self.complete_item(tender)
        with self.assertRaises(TransitionError) as caught:
            QCAudit.objects.start(tender, self.auditor)
        self.assertEqual(caught.exception.code, "r2")

    def test_the_gate_reads_the_stored_status_not_a_stale_instance(self):
        tender, _ = self.pending_audit_tender()
        force_status(type(tender).objects.get(pk=tender.pk), S.COSTING, exchange_rate_locked_at=None)
        self.assertEqual(tender.status, S.PENDING_AUDIT)  # stale in memory
        with self.assertRaises(TransitionError):
            QCAudit.objects.start(tender, self.auditor)

    def test_an_unpriced_sheet_never_gets_that_far(self):
        tender = self.costing()
        self.complete_item(tender, omit=("unit_cost",))
        with self.assertRaises(TransitionError) as caught:
            tender.submit_for_audit(self.pm)
        self.assertEqual(caught.exception.code, "r2")
        with self.assertRaises(TransitionError):
            QCAudit.objects.start(tender, self.auditor)

    def test_only_one_open_audit_per_tender(self):
        audit, tender, _ = self.open_audit()
        with self.assertRaises((ValidationError, IntegrityError)), transaction.atomic():
            QCAudit.objects.start(tender, make_user(Role.AUDITOR, "anna.m"))
        self.assertEqual(tender.qc_audits.count(), 1)


class R9aTests(AuditFixtures, TestCase):
    def frozen(self, tender):
        force_status(type(tender).objects.get(pk=tender.pk), S.PENDING_ELIGIBILITY_DECISION,
                     previous_status=S.COSTING, exchange_rate_locked_at=None)

    def test_a_frozen_tender_cannot_be_audited(self):
        tender = self.costing()
        self.complete_item(tender)
        self.frozen(tender)
        with self.assertRaises(TransitionError) as caught:
            QCAudit.objects.start(tender, self.auditor)
        self.assertEqual(caught.exception.code, "r9a")

    def test_freezing_blocks_line_edits_and_completion_of_an_open_audit(self):
        audit, tender, _ = self.open_audit()
        self.decide(audit, Verdict.POSITIVE)
        self.frozen(tender)
        line = audit.lines.get()
        line.comments = "changed while frozen"
        with self.assertRaises(TransitionError) as caught:
            line.save()
        self.assertEqual(caught.exception.code, "r9a")
        with self.assertRaises(TransitionError) as caught:
            audit.complete(self.auditor)
        self.assertEqual(caught.exception.code, "r9a")
        audit.refresh_from_db()
        self.assertEqual(audit.verdict, "")


class LineTests(AuditFixtures, TestCase):
    def test_variance_is_flagged_automatically_for_the_656_620_example(self):
        audit, *_ = self.open_audit()
        line = audit.lines.get()
        self.assertEqual(line.variance, [])  # nothing offered yet
        line.specification_offered = "Capacity 620 kg"
        line.save()
        line.refresh_from_db()
        self.assertTrue(line.has_variance)
        (flag,) = line.variance
        self.assertEqual((flag["required"], flag["offered"], flag["difference"]), ("656kg", "620 kg", "-36"))
        self.assertEqual(line.variance_summary, ["Mass: required 656kg, offered 620 kg (-36 kg, -5.49%)"])

    def test_flagging_never_sets_the_verdict(self):
        audit, *_ = self.open_audit()
        line = audit.lines.get()
        line.specification_offered = "Capacity 100 kg"  # wildly short
        line.save()
        line.refresh_from_db()
        self.assertTrue(line.has_variance)
        self.assertEqual(line.verdict, "")  # a person decides

    def test_a_flagged_line_can_still_be_positive_that_is_the_auditors_call(self):
        audit, *_ = self.open_audit()
        line = audit.lines.get()
        line.specification_offered = "Capacity 620 kg"
        line.verdict = Verdict.POSITIVE
        line.save()
        line.refresh_from_db()
        self.assertEqual((line.verdict, line.has_variance), (Verdict.POSITIVE, True))

    def test_variance_is_recomputed_on_every_save_and_cannot_be_entered(self):
        audit, *_ = self.open_audit()
        line = audit.lines.get()
        line.specification_offered = "620 kg"
        line.variance = []  # attempt to hide it
        line.save()
        line.refresh_from_db()
        self.assertEqual(len(line.variance), 1)
        line.specification_offered = "656 kg"  # corrected: flag clears
        line.variance = [{"type": "DIFFERENCE", "forged": True}]
        line.save()
        line.refresh_from_db()
        self.assertEqual(line.variance, [])

    def test_a_negative_line_needs_a_comment(self):
        audit, *_ = self.open_audit()
        line = audit.lines.get()
        line.verdict = Verdict.NEGATIVE
        for blank in ("", "   "):
            line.comments = blank
            with self.subTest(comments=blank), self.assertRaises(ValidationError):
                line.save()

    def test_an_item_from_another_tender_is_refused(self):
        audit, *_ = self.open_audit()
        stranger = self.complete_item(self.costing())
        with self.assertRaises(ValidationError):
            QCAuditLine.objects.create(audit=audit, item=stranger, specification_required="x")

    def test_one_line_per_item(self):
        audit, _, items = self.open_audit()
        with self.assertRaises((ValidationError, IntegrityError)), transaction.atomic():
            QCAuditLine.objects.create(audit=audit, item=items[0], specification_required="again")

    def test_the_specification_is_copied_so_later_item_edits_do_not_rewrite_the_audit(self):
        audit, _, (item,) = self.open_audit()
        self.assertEqual(audit.lines.get().specification_required, self.SPEC)


class CompleteTests(AuditFixtures, TestCase):
    def test_only_the_audits_own_auditor_may_complete(self):
        audit, *_ = self.open_audit()
        self.decide(audit, Verdict.POSITIVE)
        for user in (self.pm, self.md, make_user(Role.AUDITOR, "anna.m")):
            with self.subTest(user=user.email), self.assertRaises(PermissionDenied):
                audit.complete(user)

    def test_every_item_needs_a_decided_line(self):
        audit, _, items = self.open_audit("a 1 kg", "b 2 kg")
        first, second = audit.lines.order_by("pk")
        first.verdict = Verdict.POSITIVE
        first.save()
        with self.assertRaises(ValidationError) as caught:
            audit.complete(self.auditor)
        self.assertIn("no verdict yet", " ".join(caught.exception.messages))
        second.delete()  # an open audit's lines may be removed...
        with self.assertRaisesMessage(ValidationError, "Every item on the sheet needs an audit line."):
            audit.complete(self.auditor)  # ...but then it cannot be completed
        audit.refresh_from_db()
        self.assertEqual(audit.verdict, "")

    def test_all_positive_goes_to_the_md_and_keeps_the_rate_lock(self):
        audit, tender, _ = self.open_audit()
        self.decide(audit, Verdict.POSITIVE, offered="656 kg")
        audit.complete(self.auditor)
        tender.refresh_from_db()
        audit.refresh_from_db()
        self.assertEqual(audit.verdict, Verdict.POSITIVE)
        self.assertIsNotNone(audit.completed_at)
        self.assertEqual(audit.reason, "")
        self.assertEqual(tender.status, S.PENDING_EXECUTIVE_APPROVAL)
        self.assertIsNotNone(tender.exchange_rate_locked_at)

    def test_positive_sends_no_pm_email(self):
        audit, *_ = self.open_audit()
        self.decide(audit, Verdict.POSITIVE)
        with self.captureOnCommitCallbacks(execute=True):
            audit.complete(self.auditor)
        self.assertEqual(mail.outbox, [])


class R9NegativeTests(AuditFixtures, TestCase):
    def negative(self, *, reason="", specs=("Scale 656kg",)):
        audit, tender, items = self.open_audit(*specs)
        lines = list(audit.lines.order_by("pk"))
        lines[0].specification_offered = "Capacity 620 kg"
        lines[0].verdict = Verdict.NEGATIVE
        lines[0].comments = "Offered capacity is below the requirement"
        lines[0].save()
        for line in lines[1:]:
            line.verdict = Verdict.POSITIVE
            line.save()
        with self.captureOnCommitCallbacks(execute=True):
            audit.complete(self.auditor, reason=reason)
        tender.refresh_from_db()
        audit.refresh_from_db()
        return audit, tender, items

    def test_one_negative_line_returns_the_whole_tender_to_costing(self):
        audit, tender, _ = self.negative(specs=("Scale 656kg", "Autoclave 100 l"))
        self.assertEqual(audit.verdict, Verdict.NEGATIVE)
        self.assertEqual(tender.status, S.COSTING)  # the whole tender, not just the failing item

    def test_the_rate_lock_is_cleared(self):
        _, tender, _ = self.negative()
        self.assertIsNone(tender.exchange_rate_locked_at)

    def test_the_reason_is_recorded_explicit_or_taken_from_the_failing_lines(self):
        audit, *_ = self.negative(reason="  Capacity short by 36 kg  ")
        self.assertEqual(audit.reason, "Capacity short by 36 kg")
        audit, tender, items = self.negative()
        self.assertIn("Offered capacity is below the requirement", audit.reason)
        self.assertIn(items[0].item_reference, audit.reason)
        self.assertNotEqual(audit.reason.strip(), "")

    def test_the_pm_is_notified_once_with_the_reason(self):
        audit, tender, _ = self.negative(reason="Capacity short by 36 kg")
        (message,) = mail.outbox
        self.assertEqual(message.to, [tender.project_manager.email])
        self.assertIn(tender.reference, message.subject)
        self.assertIn("COSTING", message.body)
        self.assertIn("Capacity short by 36 kg", message.body)
        self.assertIn("Offered capacity is below the requirement", message.body)

    def test_the_status_change_is_audited_to_the_auditor(self):
        audit, tender, _ = self.negative()
        entry = AuditEntry.objects.filter(
            model="tenders.Tender", object_id=str(tender.pk), action=AuditAction.STATUS_CHANGE
        ).latest("id")
        self.assertEqual(entry.changes["status"], [S.PENDING_AUDIT, S.COSTING])
        self.assertEqual(entry.actor, self.auditor)

    def test_a_negative_verdict_is_not_a_dead_end(self):
        audit, tender, _ = self.negative()
        tender.submit_for_audit(self.pm)  # R2 re-runs; lock is stamped afresh (R4)
        self.assertEqual(tender.status, S.PENDING_AUDIT)
        self.assertIsNotNone(tender.exchange_rate_locked_at)
        second = QCAudit.objects.start(tender, self.auditor)  # R1 re-checked on the new audit
        self.assertNotEqual(second.pk, audit.pk)
        self.assertEqual(tender.qc_audits.count(), 2)

    def test_if_the_tender_refuses_the_whole_completion_rolls_back(self):
        audit, tender, _ = self.open_audit()
        self.decide(audit, Verdict.NEGATIVE, "bad")
        force_status(type(tender).objects.get(pk=tender.pk), S.COSTING, exchange_rate_locked_at=None)
        with self.captureOnCommitCallbacks(execute=True), self.assertRaises(TransitionError):
            audit.complete(self.auditor)
        audit.refresh_from_db()
        self.assertEqual((audit.verdict, audit.reason, audit.completed_at), ("", "", None))
        self.assertEqual(mail.outbox, [])


class ImmutabilityAndConstraintTests(AuditFixtures, TestCase):
    def completed(self, verdict=Verdict.POSITIVE):
        audit, tender, _ = self.open_audit()
        self.decide(audit, verdict, "bad" if verdict == Verdict.NEGATIVE else "")
        audit.complete(self.auditor)
        return audit

    def test_a_completed_audit_cannot_be_completed_again(self):
        audit = self.completed()
        with self.assertRaises(TransitionError):
            audit.complete(self.auditor)

    def test_a_completed_audits_lines_cannot_change(self):
        line = self.completed().lines.get()
        line.verdict = Verdict.NEGATIVE
        line.comments = "changed my mind"
        with self.assertRaisesMessage(ValidationError, "complete"):
            line.save()

    def test_a_completed_audit_and_its_lines_cannot_be_deleted_by_any_route(self):
        audit = self.completed()
        line = audit.lines.get()
        for attempt in (
            audit.delete, line.delete,
            audit.__class__.objects.filter(pk=audit.pk).delete,
            QCAuditLine.objects.filter(pk=line.pk).delete,
        ):
            with self.subTest(attempt=getattr(attempt, "__qualname__", repr(attempt))):
                with self.assertRaises(ValidationError), transaction.atomic():
                    attempt()
        self.assertTrue(QCAudit.objects.filter(pk=audit.pk).exists())
        self.assertTrue(QCAuditLine.objects.filter(pk=line.pk).exists())

    def test_an_open_audit_can_be_deleted_and_the_deletion_is_audited(self):
        audit, *_ = self.open_audit()
        pk = audit.pk
        audit.delete(actor=self.auditor)
        self.assertFalse(QCAudit.objects.filter(pk=pk).exists())
        self.assertTrue(AuditEntry.objects.filter(model="audits.QCAudit", object_id=str(pk), action="DELETE").exists())

    def test_the_verdict_cannot_be_written_outside_complete(self):
        audit, *_ = self.open_audit()
        audit.verdict = Verdict.POSITIVE
        with self.assertRaises(GuardedFieldError):
            audit.save()

    def test_an_audits_tender_and_auditor_cannot_be_swapped(self):
        audit, *_ = self.open_audit()
        audit.auditor = make_user(Role.AUDITOR, "anna.m")
        with self.assertRaises(ValidationError):
            audit.save()

    def test_queryset_update_is_refused(self):
        from core.models import AuditBypassError

        audit, *_ = self.open_audit()
        with self.assertRaises(AuditBypassError):
            QCAudit.objects.filter(pk=audit.pk).update(verdict="POSITIVE")

    def sql(self, statement, *params):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(statement, params)

    def test_database_refuses_a_negative_audit_without_a_reason(self):
        audit, *_ = self.open_audit()
        self.sql("UPDATE audits_qcaudit SET verdict='NEGATIVE', completed_at=now(), reason='' WHERE id=%s", audit.pk)

    def test_database_refuses_a_verdict_without_completion_and_vice_versa(self):
        audit, *_ = self.open_audit()
        self.sql("UPDATE audits_qcaudit SET verdict='POSITIVE' WHERE id=%s", audit.pk)
        self.sql("UPDATE audits_qcaudit SET completed_at=now() WHERE id=%s", audit.pk)

    def test_database_refuses_an_unknown_verdict(self):
        audit, *_ = self.open_audit()
        self.sql("UPDATE audits_qcaudit SET verdict='MAYBE' WHERE id=%s", audit.pk)
        self.sql("UPDATE audits_qcauditline SET verdict='MAYBE' WHERE audit_id=%s", audit.pk)

    def test_database_refuses_a_negative_line_without_comments(self):
        audit, *_ = self.open_audit()
        self.sql("UPDATE audits_qcauditline SET verdict='NEGATIVE', comments='' WHERE audit_id=%s", audit.pk)

    def test_database_refuses_a_second_open_audit_for_a_tender(self):
        audit, tender, _ = self.open_audit()
        self.sql(
            "INSERT INTO audits_qcaudit (tender_id, auditor_id, verdict, reason, created_at, updated_at) "
            "VALUES (%s, %s, '', '', now(), now())", tender.pk, self.auditor.pk,
        )


class WiringTests(AuditFixtures, TestCase):
    def test_the_audit_is_audited_per_change_with_field_level_changes(self):
        audit, *_ = self.open_audit()
        line = audit.lines.get()
        line.specification_offered = "620 kg"
        line.save()
        entry = AuditEntry.objects.filter(model="audits.QCAuditLine", object_id=str(line.pk)).latest("id")
        self.assertEqual(entry.action, AuditAction.UPDATE)
        self.assertEqual(entry.changes["specification_offered"], ["", "620 kg"])
        self.assertIn("variance", entry.changes)  # the flag is on the trail too

    def test_admin_is_read_only(self):
        audit, *_ = self.open_audit()
        model_admin = admin.site._registry[QCAudit]
        request = RequestFactory().get("/")
        request.user = self.admin_user
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, audit))
        self.assertFalse(model_admin.has_delete_permission(request, audit))
