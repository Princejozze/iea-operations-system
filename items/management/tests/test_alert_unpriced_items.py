"""alert_unpriced_items: the T-3 / T-2 / T-1 ladder (CLAUDE.md §10)."""

from decimal import Decimal

from accounts.models import Role
from tenders.management.tests.alert_fixtures import AlertTestCase, add_item, ledger, local_noon, make_user, set_state
from tenders.models import TenderStatus as S

CMD = "alert_unpriced_items"


class UnpricedLadderTests(AlertTestCase):
    def tender_due_in(self, days, *, status=S.COSTING, priced=0, unpriced=1, **kw):
        tender = self.new_tender(S.COSTING, submission_deadline=local_noon(days), **kw)
        for _ in range(priced):
            add_item(tender, unit_cost=Decimal("100"))
        for _ in range(unpriced):
            add_item(tender)
        if status != S.COSTING:
            extra = {"previous_status": S.COSTING} if status == S.PENDING_ELIGIBILITY_DECISION else {}
            set_state(tender, status, **extra)
        return tender

    def test_t3_goes_to_the_pm_only(self):
        tender = self.tender_due_in(3, priced=2, unpriced=1)
        [msg] = self.run_command(CMD)
        self.assertEqual(msg.to, [self.pm.email])
        self.assertIn(tender.reference, msg.subject)
        self.assertIn("T-3", msg.subject)
        self.assertIn("1 of 3 items still has no unit cost", msg.body)
        self.assertEqual(ledger("UNPRICED_T_MINUS_3", tender).count(), 1)

    def test_t2_adds_head_of_operations(self):
        self.tender_due_in(2)
        [msg] = self.run_command(CMD)
        self.assertEqual(msg.to, [self.pm.email, self.hops.email])

    def test_t1_broadcasts_to_management_team(self):
        tender = self.tender_due_in(1, unpriced=2)
        [msg] = self.run_command(CMD)
        self.assertEqual(sorted(msg.to), sorted([self.md.email, self.hops.email, self.pm.email]))
        self.assertIn("JEOPARDY", msg.subject)
        self.assertIn("jeopardises the whole bid", msg.body)
        for outsider in (self.pm2, self.auditor, self.finance, self.admin_user):
            self.assertNotIn(outsider.email, msg.to)
        self.assertIsNone(ledger("UNPRICED_T_MINUS_1", tender).get().actor)

    def test_inactive_users_are_left_out(self):
        make_user(Role.HEAD_OF_OPERATIONS, "former", is_active=False)
        self.tender_due_in(2)
        [msg] = self.run_command(CMD)
        self.assertNotIn("former@imperialeastafrica.tz", msg.to)

    def test_outside_the_ladder_nothing_is_sent(self):
        for days in (4, 10, 0, -1):
            self.tender_due_in(days)
        self.assertEqual(self.run_command(CMD), [])

    def test_fully_priced_tender_is_quiet(self):
        self.tender_due_in(1, priced=3, unpriced=0)
        self.assertEqual(self.run_command(CMD), [])

    def test_tender_with_no_deadline_is_skipped(self):
        tender = self.new_tender(S.COSTING)
        add_item(tender)
        self.assertEqual(self.run_command(CMD), [])

    def test_only_open_for_pricing_states(self):
        self.tender_due_in(1, status=S.DRAFTING)
        frozen = self.tender_due_in(1, status=S.PENDING_ELIGIBILITY_DECISION)
        self.tender_due_in(1, status=S.WITHDRAWN)
        self.tender_due_in(1, status=S.PENDING_AUDIT)
        sent = self.run_command(CMD)
        self.assertEqual(len(sent), 2)
        frozen_msg = next(m for m in sent if frozen.reference in m.subject)
        self.assertIn("frozen", frozen_msg.body)

    def test_idempotent_same_day(self):
        tender = self.tender_due_in(1)
        self.assertEqual(len(self.run_command(CMD)), 1)
        self.assertEqual(self.run_command(CMD), [])
        self.assertEqual(self.run_command(CMD), [])
        self.assertEqual(ledger("UNPRICED_T_MINUS_1", tender).count(), 1)

    def test_one_email_per_tender(self):
        self.tender_due_in(3)
        self.tender_due_in(3, pm=self.pm2)
        sent = self.run_command(CMD)
        self.assertEqual({m.to[0] for m in sent}, {self.pm.email, self.pm2.email})
        self.assertEqual(ledger("UNPRICED_T_MINUS_3").count(), 2)
