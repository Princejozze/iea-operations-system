"""
EligibilityReview (§7), R16 (an unmet requirement escalates to the MD) and
R9a (a tender in PENDING_ELIGIBILITY_DECISION is frozen).
"""

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from approvals.models import EligibilityDecision as E, EligibilityReview
from suppliers.models import Supplier
from tenders.models import ELIGIBILITY_ENTRY_STATUSES, GuardedFieldError, TenderStatus as S, TransitionError

from .helpers import TenderFixtures, force_status, status_entries

REQUIREMENT = "Annual turnover of at least TZS 2 billion."


class RaiseTests(TenderFixtures, TestCase):
    def raise_review(self, tender, user=None):
        return EligibilityReview.objects.raise_for(
            tender, user or self.pm, requirement=REQUIREMENT, pm_notes="Last year we did 1.4bn."
        )

    def test_pm_escalates_from_drafting_or_costing(self):
        for origin in ELIGIBILITY_ENTRY_STATUSES:
            with self.subTest(origin=origin):
                tender = self.new_tender(origin)
                review = self.raise_review(tender)
                tender.refresh_from_db()
                self.assertEqual(tender.status, S.PENDING_ELIGIBILITY_DECISION)
                self.assertEqual(tender.previous_status, origin)
                self.assertTrue(review.is_open)
                self.assertEqual((review.raised_by, review.requirement), (self.pm, REQUIREMENT))
                self.assertEqual(status_entries(tender).last().actor, self.pm)

    def test_only_this_tenders_pm_can_raise(self):
        tender = self.new_tender(S.DRAFTING)
        for user in (self.md, self.hops, self.auditor, self.finance, self.admin_user, self.pm2):
            with self.subTest(role=user.role, who=user.first_name), self.assertRaises((PermissionDenied, ValidationError)):
                self.raise_review(tender, user)
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.DRAFTING)
        self.assertFalse(EligibilityReview.objects.exists())

    def test_it_cannot_be_raised_from_any_other_state(self):
        for status in (S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL,
                       S.PENDING_AUDIT, S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT,
                       S.PENDING_ELIGIBILITY_DECISION, S.WITHDRAWN):
            with self.subTest(status=status):
                extra = {"previous_status": S.DRAFTING} if status == S.PENDING_ELIGIBILITY_DECISION else {}
                tender = self.new_tender(status, **extra)
                with self.assertRaises((TransitionError, ValidationError)):
                    self.raise_review(tender)
        self.assertFalse(EligibilityReview.objects.exists())

    def test_a_review_needs_a_requirement(self):
        tender = self.new_tender(S.DRAFTING)
        with self.assertRaises(ValidationError):
            EligibilityReview.objects.raise_for(tender, self.pm, requirement="  ")
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.DRAFTING)

    # R16 gate, both directions: the transition itself.
    def test_the_transition_refuses_without_a_review(self):
        tender = self.new_tender(S.DRAFTING)
        with self.assertRaises(TransitionError) as ctx:
            tender.raise_eligibility(self.pm)
        self.assertIn("R16", str(ctx.exception))
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.DRAFTING)

    def test_the_transition_succeeds_with_an_open_review(self):
        tender = self.new_tender(S.COSTING)
        EligibilityReview(tender=tender, requirement=REQUIREMENT, raised_by=self.pm).save()
        tender.raise_eligibility(self.pm)
        self.assertEqual(tender.status, S.PENDING_ELIGIBILITY_DECISION)
        self.assertEqual(tender.previous_status, S.COSTING)

    def test_a_decided_review_does_not_count_for_a_new_escalation(self):
        tender = self.new_tender(S.DRAFTING)
        first = self.raise_review(tender)
        first.decide(self.md, E.PROCEED)
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.DRAFTING)
        with self.assertRaises(TransitionError):
            tender.raise_eligibility(self.pm)  # only a decided review exists: nothing open to escalate

    def test_two_open_reviews_cannot_coexist(self):
        tender = self.new_tender(S.DRAFTING)
        self.raise_review(tender)
        with self.assertRaises(ValidationError):
            EligibilityReview(tender=tender, requirement="Another", raised_by=self.pm).save()


class DecideTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.COSTING)
        self.review = EligibilityReview.objects.raise_for(self.tender, self.pm, requirement=REQUIREMENT)
        self.partner = Supplier.objects.create(name="Kilimanjaro Labs Ltd")

    def test_only_exec_can_resolve(self):
        for user in (self.pm, self.pm2, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role, who=user.first_name), self.assertRaises(PermissionDenied):
                self.review.decide(user, E.PROCEED)
        self.review.refresh_from_db()
        self.tender.refresh_from_db()
        self.assertTrue(self.review.is_open)
        self.assertEqual(self.tender.status, S.PENDING_ELIGIBILITY_DECISION)

    def test_proceed_and_remediate_return_to_where_it_came_from(self):
        for decision in (E.PROCEED, E.REMEDIATE):
            for origin in ELIGIBILITY_ENTRY_STATUSES:
                with self.subTest(decision=decision, origin=origin):
                    tender = self.new_tender(origin)
                    review = EligibilityReview.objects.raise_for(tender, self.pm, requirement=REQUIREMENT)
                    review.decide(self.md, decision, notes="Noted.")
                    tender.refresh_from_db()
                    self.assertEqual(tender.status, origin)
                    self.assertEqual(tender.previous_status, "")
                    self.assertEqual(review.md_decision, decision)
                    self.assertEqual((review.decided_by, review.decision_notes), (self.md, "Noted."))
                    self.assertIsNotNone(review.decided_at)

    def test_joint_venture_names_the_partner_and_returns_the_tender(self):
        self.review.decide(self.md, E.JOINT_VENTURE, jv_partner=self.partner, notes="Partner on turnover.")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.COSTING)
        self.assertEqual(self.review.jv_partner, self.partner)

    def test_withdraw_ends_the_tender(self):
        self.review.decide(self.md, E.WITHDRAW, notes="Cannot meet it.")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.WITHDRAWN)
        self.assertEqual(self.tender.previous_status, "")

    def test_the_partner_rule(self):
        with self.assertRaises(ValidationError):
            self.review.decide(self.md, E.JOINT_VENTURE)  # JV without a partner
        for decision in (E.PROCEED, E.REMEDIATE, E.WITHDRAW):
            with self.subTest(decision=decision), self.assertRaises(ValidationError):
                self.review.decide(self.md, decision, jv_partner=self.partner)  # partner on a non-JV
        with self.assertRaises(ValidationError):
            self.review.decide(self.md, "MAYBE")
        self.review.refresh_from_db()
        self.assertTrue(self.review.is_open)

    def test_a_review_is_decided_once(self):
        self.review.decide(self.md, E.PROCEED)
        with self.assertRaises(TransitionError):
            self.review.decide(self.md, E.WITHDRAW)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.COSTING)

    def test_the_pm_is_told_of_the_decision(self):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.review.decide(self.md, E.REMEDIATE, notes="Get the audited accounts certified.")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.pm.email])
        self.assertIn("remediate", mail.outbox[0].body)
        self.assertIn("Get the audited accounts certified.", mail.outbox[0].body)

    # R16 gate on the transition itself, both directions.
    def test_resolve_refuses_without_a_decision(self):
        with self.assertRaises(TransitionError):
            self.tender.resolve_eligibility(self.md)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_ELIGIBILITY_DECISION)

    def test_an_earlier_decision_cannot_resolve_a_later_escalation(self):
        self.review.decide(self.md, E.PROCEED)
        EligibilityReview.objects.raise_for(self.tender, self.pm, requirement="Second requirement.")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_ELIGIBILITY_DECISION)
        with self.assertRaises(TransitionError):
            self.tender.resolve_eligibility(self.md)  # the new review is still open
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_ELIGIBILITY_DECISION)

    def test_decision_fields_are_written_only_by_decide(self):
        self.review.md_decision = E.PROCEED
        with self.assertRaises(GuardedFieldError):
            self.review.save()
        with self.assertRaises(GuardedFieldError):
            EligibilityReview(tender=self.new_tender(S.DRAFTING), requirement="x", raised_by=self.pm,
                              md_decision=E.PROCEED, decided_by=self.md, decided_at=timezone.now()).save()


class R9aFreezeTests(TenderFixtures, TestCase):
    """A tender awaiting the MD's eligibility decision is frozen: only that
    decision (or an MD withdrawal) moves it."""

    def setUp(self):
        self.tender = self.new_tender(S.COSTING)
        self.review = EligibilityReview.objects.raise_for(self.tender, self.pm, requirement=REQUIREMENT)

    def assertStillFrozen(self):
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_ELIGIBILITY_DECISION)
        self.assertTrue(self.tender.is_frozen)

    def test_every_other_transition_is_refused(self):
        attempts = {
            "finish_extraction": lambda: self.tender.finish_extraction(),
            "confirm_extraction": lambda: self.tender.confirm_extraction(self.pm),
            "approve_registration": lambda: self.tender.approve_registration(self.md),
            "start_costing": lambda: self.tender.start_costing(self.pm),
            "submit_for_audit": lambda: self.tender.submit_for_audit(self.pm),
            "record_audit_result": lambda: self.tender.record_audit_result(self.auditor, compliant=True),
            "approve_submission": lambda: self.tender.approve_submission(self.md),
            "mark_submitted": lambda: self.tender.mark_submitted(self.pm),
            "record_outcome": lambda: self.tender.record_outcome(self.pm, award_status="AWARDED"),
            "raise_eligibility": lambda: self.tender.raise_eligibility(self.pm),
        }
        for name, attempt in attempts.items():
            with self.subTest(transition=name), self.assertRaises(TransitionError):
                attempt()
        self.assertStillFrozen()

    def test_no_work_is_allowed_and_the_guard_refuses(self):
        self.tender.refresh_from_db()
        self.assertFalse(self.tender.work_allowed)
        with self.assertRaises(TransitionError) as ctx:
            self.tender.require_unfrozen("Pricing an item")
        self.assertEqual(ctx.exception.code, "r9a")
        self.assertIn("R9a", str(ctx.exception))

    def test_the_guard_reads_the_stored_status_not_a_stale_copy(self):
        stale = type(self.tender).objects.get(pk=self.tender.pk)
        self.review.decide(self.md, E.PROCEED)  # unfreezes in the database
        stale.require_unfrozen("Pricing an item")  # stale.status still says frozen; the database says not
        other = self.new_tender(S.DRAFTING)
        copy = type(other).objects.get(pk=other.pk)
        EligibilityReview.objects.raise_for(other, self.pm, requirement=REQUIREMENT)
        with self.assertRaises(TransitionError):
            copy.require_unfrozen("Pricing an item")  # copy.status says DRAFTING; the database says frozen

    def test_work_resumes_when_the_md_resolves(self):
        self.review.decide(self.md, E.PROCEED)
        self.tender.refresh_from_db()
        self.assertFalse(self.tender.is_frozen)
        self.assertTrue(self.tender.work_allowed)
        self.tender.require_unfrozen("Pricing an item")  # no error

    def test_the_guard_is_quiet_on_every_unfrozen_state(self):
        for status in S.values:
            if status == S.PENDING_ELIGIBILITY_DECISION:
                continue
            with self.subTest(status=status):
                self.new_tender(status).require_unfrozen("anything")

    def test_no_approval_can_be_signed_while_frozen(self):
        from approvals.models import ApprovalDecision, ApprovalKind
        from .helpers import sign

        for kind in ApprovalKind.values:
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                sign(self.tender, kind, ApprovalDecision.APPROVED, self.md)

    def test_only_the_mds_decision_or_withdrawal_moves_it(self):
        for user in (self.pm, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role), self.assertRaises(PermissionDenied):
                self.tender.resolve_eligibility(user)
                self.tender.withdraw(user)
        self.assertStillFrozen()
        self.tender.withdraw(self.md)  # §8: EXEC may withdraw from any state before SUBMITTED
        self.assertEqual(self.tender.status, S.WITHDRAWN)
