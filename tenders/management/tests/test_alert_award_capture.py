"""alert_award_capture: prompt for the outcome once bids are opened (CLAUDE.md §10, §12)."""

from datetime import timedelta

from django.utils import timezone

from core.models import AuditAction, AuditEntry
from tenders.management.tests.alert_fixtures import AlertTestCase, ledger, local_noon
from tenders.models import AwardStatus, BidOpeningPosition, TenderOutcome
from tenders.models import TenderStatus as S

CMD = "alert_award_capture"
KIND = "AWARD_CAPTURE"


class AwardCaptureTests(AlertTestCase):
    def opened(self, days_ago=1, **kw):
        return self.new_tender(S.SUBMITTED, submission_deadline=local_noon(-days_ago), **kw)

    def backdate_prompt(self, tender, days_ago):
        AuditEntry.objects.create(
            action=AuditAction.CREATE, model=f"alert.{KIND}", object_id=str(tender.pk),
            object_repr=str(tender), timestamp=timezone.now() - timedelta(days=days_ago),
        )

    def test_prompts_pm_after_opening(self):
        tender = self.opened()
        [msg] = self.run_command(CMD)
        self.assertEqual(msg.to, [self.pm.email])
        self.assertIn(tender.reference, msg.subject)
        self.assertIn("position at opening", msg.body)
        self.assertEqual(ledger(KIND, tender).count(), 1)

    def test_not_before_the_deadline(self):
        self.new_tender(S.SUBMITTED, submission_deadline=timezone.now() + timedelta(hours=1))
        self.assertEqual(self.run_command(CMD), [])

    def test_same_day_after_opening_counts(self):
        self.new_tender(S.SUBMITTED, submission_deadline=timezone.now() - timedelta(minutes=5))
        self.assertEqual(len(self.run_command(CMD)), 1)

    def test_only_submitted_tenders(self):
        for status in (S.READY_TO_SUBMIT, S.WON, S.LOST, S.WITHDRAWN):
            self.new_tender(status, submission_deadline=local_noon(-2))
        self.assertEqual(self.run_command(CMD), [])

    def test_pending_outcome_with_position_still_prompts_for_award(self):
        tender = self.opened()
        tender.record_outcome(self.pm, award_status=AwardStatus.PENDING,
                              bid_opening_position=BidOpeningPosition.LOWEST)
        [msg] = self.run_command(CMD)
        self.assertIn("award result is still pending", msg.body)
        self.assertIn("Lowest", msg.body)

    def test_recorded_award_silences_it(self):
        tender = self.opened()
        tender.record_outcome(self.pm, award_status=AwardStatus.AWARDED)  # -> WON
        self.assertEqual(self.run_command(CMD), [])

    def test_inactive_pm_falls_back_to_md(self):
        tender = self.opened(pm=self.pm2)
        self.pm2.is_active = False
        self.pm2.save()
        [msg] = self.run_command(CMD)
        self.assertEqual(msg.to, [self.md.email])
        self.assertIn(tender.reference, msg.subject)

    def test_idempotent_same_day(self):
        tender = self.opened()
        self.assertEqual(len(self.run_command(CMD)), 1)
        self.assertEqual(self.run_command(CMD), [])
        self.assertEqual(ledger(KIND, tender).count(), 1)

    def test_repeats_weekly_until_recorded(self):
        tender = self.opened(days_ago=20)
        self.backdate_prompt(tender, days_ago=3)
        self.assertEqual(self.run_command(CMD), [])
        self.assertEqual(len(self.run_command(CMD, "--every", "3")), 1)

    def test_repeat_after_seven_days(self):
        tender = self.opened(days_ago=20)
        self.backdate_prompt(tender, days_ago=7)
        [msg] = self.run_command(CMD)
        self.assertIn("Reminder", msg.body)
