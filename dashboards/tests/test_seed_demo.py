"""
seed_demo: refuses outside DEBUG, reaches every state through the real rules, and is idempotent.
"""

from io import StringIO

from django.conf import settings
from django.contrib.auth import authenticate
from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import CommandError, call_command
from django.test import TestCase, TransactionTestCase, override_settings

from accounts.models import Role, User
from approvals.models import Approval, FundRequest
from audits.models import QCAudit
from core.models import AuditEntry
from dashboards.management.commands.seed_demo import DEMO_USERS, PLANS
from tenders.models import Tender, TenderStatus as S


def seed():
    out = StringIO()
    with override_settings(DEBUG=True):
        call_command("seed_demo", stdout=out)
    return out.getvalue()


class RefusesOutsideDebugTests(TestCase):
    def test_it_refuses_to_run_with_debug_off_and_writes_nothing(self):
        self.assertFalse(settings.DEBUG)  # the test runner forces it off
        with self.assertRaisesMessage(CommandError, "DEBUG"):
            call_command("seed_demo", stdout=StringIO())
        self.assertFalse(User.objects.exists())
        self.assertFalse(Tender.objects.exists())


class SeedDemoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.first_output = seed()
        cls.by_key = {p.key: Tender.objects.get(documents__original_filename=f"demo-{p.key}.pdf") for p in PLANS}

    def status(self, key):
        return self.by_key[key].status

    def test_one_user_per_role_with_the_printed_passwords(self):
        self.assertEqual({u.role for u in User.objects.all()}, set(Role.values))
        for demo in DEMO_USERS:
            self.assertIsNotNone(authenticate(username=demo.email, password=demo.password), demo.email)
            self.assertIn(demo.email, self.first_output)
            self.assertIn(demo.password, self.first_output)
        self.assertFalse(User.objects.filter(must_change_password=True).exists())

    def test_every_requested_state_is_reached(self):
        self.assertEqual(
            {key: self.status(key) for key in self.by_key},
            {
                "verify": S.PENDING_VERIFICATION, "registration": S.PENDING_REGISTRATION_APPROVAL,
                "costing": S.COSTING, "audit": S.PENDING_AUDIT, "exec": S.PENDING_EXECUTIVE_APPROVAL,
                "ready": S.READY_TO_SUBMIT, "won": S.WON, "lost": S.LOST,
            },
        )

    def test_the_verification_tender_has_a_draft_with_confidences_and_gaps(self):
        tender = self.by_key["verify"]
        fields = tender.extraction.fields
        self.assertEqual(tender.title, "")  # nothing reaches the tender until the PM confirms (R12)
        self.assertIsNone(tender.client)
        self.assertEqual(fields["submission_deadline"]["confidence"], 0.96)
        self.assertTrue(fields["client_address"]["low_confidence"])
        self.assertIsNone(fields["clarification_deadline"]["value"])  # §5: never inferred
        self.assertIsNone(fields["site_visit_required"]["value"])
        self.assertIn("DEMO", tender.extraction.message)

    def test_the_costing_tender_has_a_mix_of_priced_unpriced_and_quoted_items(self):
        items = list(self.by_key["costing"].items.all())
        self.assertEqual(len(items), 15)
        self.assertEqual(sum(1 for i in items if i.is_costed), 7)
        self.assertEqual(sum(1 for i in items if i.selected_quote_id), 5)
        with_quotes = [i for i in items if i.quotes.exists()]
        self.assertEqual(len(with_quotes), 8)
        self.assertEqual(sum(1 for i in with_quotes if not i.is_costed), 3)  # quoted, not yet chosen
        self.assertEqual(sum(1 for i in items if not i.quotes.exists() and not i.is_costed), 5)
        self.assertIsNone(self.by_key["costing"].exchange_rate_locked_at)

    def test_the_audit_tender_shows_variance_flags(self):
        tender = self.by_key["audit"]
        self.assertIsNotNone(tender.exchange_rate_locked_at)  # R4
        audit = tender.qc_audits.get()
        self.assertFalse(audit.is_complete)
        self.assertNotEqual(audit.auditor_id, tender.project_manager_id)  # R1
        flagged = [line for line in audit.lines.all() if line.has_variance]
        self.assertEqual(len(flagged), 3)
        self.assertEqual(audit.lines.count(), 6)
        self.assertTrue(all(not line.verdict for line in audit.lines.all()))  # the verdict stays the auditor's

    def test_the_exec_tender_is_ready_to_sign(self):
        tender = self.by_key["exec"]
        self.assertTrue(tender.qc_audits.get().is_complete)
        self.assertFalse(tender.approvals.filter(kind="SUBMISSION").exists())
        self.assertEqual(tender.fund_requests.get().status, "PENDING")

    def test_the_ready_tender_is_blocked_by_an_unpaid_fund_request(self):
        tender = self.by_key["ready"]
        self.assertTrue(tender.approvals.filter(kind="SUBMISSION", decision="APPROVED").exists())
        request = tender.fund_requests.get()
        self.assertEqual((request.purpose, request.status), ("NEST_APPLICATION_FEE", "APPROVED"))
        self.assertFalse(tender.has_nest_funds)  # R17

    def test_won_and_lost_have_outcomes_and_paid_funds(self):
        won, lost = self.by_key["won"], self.by_key["lost"]
        self.assertEqual((won.outcome.award_status, won.outcome.bid_opening_position), ("AWARDED", "LOWEST"))
        self.assertGreater(won.outcome.contract_sum, 0)
        self.assertEqual((lost.outcome.award_status, lost.outcome.bid_opening_position), ("NOT_AWARDED", "THIRD_LOWEST"))
        self.assertTrue(won.has_nest_funds and lost.has_nest_funds)
        self.assertEqual(lost.project_manager.email, "pm2@demo.invalid")

    def test_status_changes_were_audited_through_the_transition_methods(self):
        won = self.by_key["won"]
        changes = AuditEntry.objects.filter(
            model="tenders.Tender", object_id=str(won.pk), action="STATUS_CHANGE"
        ).count()
        # finish_extraction, confirm, registration, costing, audit, audit result, submission, submitted, outcome
        self.assertEqual(changes, 9)
        self.assertEqual(Approval.objects.filter(tender=won).count(), 2)

    def test_it_is_idempotent(self):
        before = {
            "tenders": Tender.objects.count(), "users": User.objects.count(), "audit": AuditEntry.objects.count(),
            "funds": FundRequest.objects.count(), "audits": QCAudit.objects.count(),
            "references": sorted(Tender.objects.values_list("reference", flat=True)),
        }
        second = seed()
        self.assertEqual(before, {
            "tenders": Tender.objects.count(), "users": User.objects.count(), "audit": AuditEntry.objects.count(),
            "funds": FundRequest.objects.count(), "audits": QCAudit.objects.count(),
            "references": sorted(Tender.objects.values_list("reference", flat=True)),
        })
        self.assertNotIn("created ", second)
        self.assertEqual(second.count("exists"), len(PLANS))

    def test_a_demo_tender_the_user_has_moved_on_is_left_alone(self):
        tender = self.by_key["registration"]
        md = User.objects.get(email="md@demo.invalid")
        tender.withdraw(md)
        seed()
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.WITHDRAWN)

    def test_a_changed_demo_password_is_put_back(self):
        user = User.objects.get(email="fin@demo.invalid")
        user.set_password("something-else")
        user.save()
        seed()
        self.assertIsNotNone(authenticate(username="fin@demo.invalid", password="Demo-fin-2026"))


class ExplodingBackend(BaseEmailBackend):
    """Fails loudly if any mail is handed to it."""

    def send_messages(self, email_messages):
        raise AssertionError(f"seed_demo sent real mail: {[m.subject for m in email_messages]}")


class NoRealMailTests(TransactionTestCase):
    """A real commit per tender, as in production: inside TestCase every notice would be deferred
    until after the command (and its in-memory mail override) had finished, which proves nothing."""

    def test_no_notice_reaches_the_configured_backend_and_the_setting_is_restored(self):
        with override_settings(EMAIL_BACKEND=f"{__name__}.ExplodingBackend"):
            seed()  # ExplodingBackend raises from on_commit if anything is routed to it
            self.assertEqual(settings.EMAIL_BACKEND, f"{__name__}.ExplodingBackend")
        self.assertEqual(Tender.objects.count(), len(PLANS))
        self.assertTrue(mail.outbox)  # the notices went to memory, not nowhere
