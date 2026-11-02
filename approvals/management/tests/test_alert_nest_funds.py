"""alert_nest_funds: the NeST funds-not-loaded warning (CLAUDE.md §10, R17)."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from approvals.models import FundRequest, FundRequestPurpose
from tenders.management.tests.alert_fixtures import AlertTestCase, ledger, local_noon
from tenders.models import TenderStatus as S

CMD = "alert_nest_funds"
KIND = "NEST_FUNDS_NOT_LOADED"


class NestFundsAlertTests(AlertTestCase):
    def tender_due_in(self, days, status=S.READY_TO_SUBMIT, **kw):
        return self.new_tender(status, submission_deadline=local_noon(days), **kw)

    def fund_request(self, tender, purpose=FundRequestPurpose.NEST_APPLICATION_FEE, *, to="PENDING"):
        fr = FundRequest.objects.create(
            tender=tender, purpose=purpose, amount=Decimal("50000"), currency="TZS", requested_by=self.pm,
        )
        if to in ("APPROVED", "PAID"):
            fr.approve(self.md)
        if to == "PAID":
            fr.mark_paid(self.finance, "BANK-REF-1")
        if to == "DECLINED":
            fr.decline(self.md, "Not this one")
        return fr

    def test_unfunded_with_no_request_tells_pm_hops_and_md(self):
        tender = self.tender_due_in(2)
        [msg] = self.run_command(CMD)
        self.assertEqual(sorted(msg.to), sorted([self.pm.email, self.hops.email, self.md.email]))
        self.assertIn("in 2 days", msg.subject)
        self.assertIn("No NeST application-fee request has been raised", msg.body)
        self.assertEqual(ledger(KIND, tender).count(), 1)

    def test_pending_request_goes_to_md(self):
        tender = self.tender_due_in(1, status=S.COSTING)
        self.fund_request(tender)
        [msg] = self.run_command(CMD)
        self.assertIn(self.md.email, msg.to)
        self.assertNotIn(self.finance.email, msg.to)
        self.assertIn("waiting for the Managing Director", msg.body)

    def test_approved_unpaid_request_goes_to_finance(self):
        tender = self.new_tender(S.COSTING, submission_deadline=timezone.now() + timedelta(minutes=30))
        self.fund_request(tender, to="APPROVED")
        [msg] = self.run_command(CMD)
        self.assertEqual(sorted(msg.to), sorted([self.pm.email, self.hops.email, self.finance.email]))
        self.assertIn("waiting for Finance", msg.body)

    def test_declined_request_is_not_funding(self):
        tender = self.tender_due_in(1, status=S.COSTING)
        self.fund_request(tender, to="DECLINED")
        [msg] = self.run_command(CMD)
        self.assertIn("declined", msg.body)

    def test_paid_nest_fee_silences_it(self):
        tender = self.tender_due_in(1, status=S.COSTING)
        self.fund_request(tender, to="PAID")
        self.assertEqual(self.run_command(CMD), [])

    def test_paid_bid_security_does_not_count(self):
        tender = self.tender_due_in(1, status=S.COSTING)
        self.fund_request(tender, FundRequestPurpose.BID_SECURITY, to="PAID")
        self.assertEqual(len(self.run_command(CMD)), 1)

    def test_window(self):
        self.tender_due_in(4)
        self.assertEqual(self.run_command(CMD), [])
        self.assertEqual(len(self.run_command(CMD, "--days", "5")), 1)

    def test_past_deadline_and_terminal_states_are_skipped(self):
        self.tender_due_in(-1)
        for status in (S.SUBMITTED, S.WITHDRAWN, S.WON, S.LOST, S.PENDING_REGISTRATION_APPROVAL):
            self.tender_due_in(1, status=status)
        self.assertEqual(self.run_command(CMD), [])

    def test_idempotent_same_day(self):
        tender = self.tender_due_in(1)
        self.assertEqual(len(self.run_command(CMD)), 1)
        self.assertEqual(self.run_command(CMD), [])
        self.assertEqual(ledger(KIND, tender).count(), 1)
