"""
NeST funds-not-loaded warning (CLAUDE.md §10 "Alerts", R17).

A bid cannot be submitted until the NeST account holds the application fee:
``mark_submitted`` refuses while ``Tender.has_nest_funds`` is false, and that is
true only for a PAID fund request with purpose NEST_APPLICATION_FEE. A paid
BID_SECURITY or OTHER request does not count, and nor does an APPROVED one
Finance has not yet paid.

This warns, once per tender per day, while a tender is within ``--days``
calendar days of its submission deadline (default 3) and is not funded. The
email names where the money is stuck and goes to whoever must act next:

    no request, or only declined ones   PM (raise one) and the MD
    request PENDING                     the MD (approve it)
    request APPROVED, not paid          Finance (pay it)

The PM and the Head of Operations receive every one.

Cron, daily in the early morning:

    5 7 * * *  cd /srv/iea-system && .venv/bin/python manage.py alert_nest_funds >> /var/log/iea/alerts.log 2>&1
"""

from django.utils import timezone

from accounts.models import Role, User
from approvals.models import FundRequest, FundRequestPurpose, FundRequestStatus
from core.management.alerts import AlertCommand, clean_recipients, days_until, local_today, tender_header
from tenders.models import Tender, TenderStatus

S = TenderStatus
FS = FundRequestStatus

# Every state before SUBMITTED in which the bid is still being pursued.
PRE_SUBMISSION = (
    S.DRAFTING, S.COSTING, S.PENDING_ELIGIBILITY_DECISION, S.PENDING_AUDIT,
    S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT,
)
KIND = "NEST_FUNDS_NOT_LOADED"


def _users(role):
    return list(User.objects.filter(role=role, is_active=True).order_by("pk"))


class Command(AlertCommand):
    help = "Warn when a tender nears its deadline without a PAID NeST application-fee fund request. Run daily."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=3,
                            help="Warn from this many calendar days before the deadline (default 3).")

    def handle_alerts(self, *, days, **options):
        today = local_today()
        tenders = (
            Tender.objects.filter(
                status__in=PRE_SUBMISSION,
                submission_deadline__gt=timezone.now(),
            )
            .exclude(
                fund_requests__purpose=FundRequestPurpose.NEST_APPLICATION_FEE,
                fund_requests__status=FS.PAID,
            )
            .select_related("project_manager", "client")
            .order_by("submission_deadline")
        )
        cache = {}

        def users(role):
            if role not in cache:
                cache[role] = _users(role)
            return cache[role]

        for tender in tenders:
            left = days_until(tender.submission_deadline, today)
            if left > days:
                continue
            state, line = self._funding_state(tender)
            acting = {"none": users(Role.EXECUTIVE), FS.PENDING: users(Role.EXECUTIVE),
                      FS.APPROVED: users(Role.FINANCE)}[state]
            audience = [tender.project_manager, *users(Role.HEAD_OF_OPERATIONS), *acting]
            when = {0: "today", 1: "tomorrow"}.get(left, f"in {left} days")
            self.deliver(
                kind=KIND, tender=tender, recipients=clean_recipients(audience),
                subject=f"NeST not funded — {tender.reference} deadline {when}",
                body=(
                    f"{tender_header(tender)}\n\n"
                    f"The submission deadline is {when}, and the NeST account has not been funded for this "
                    f"tender: there is no PAID fund request for the NeST application fee. The bid cannot be "
                    f"submitted until there is (R17).\n\n{line}\n"
                ),
            )

    @staticmethod
    def _funding_state(tender):
        """Where the NEST_APPLICATION_FEE money is stuck, as (state, sentence)."""
        requests = list(
            FundRequest.objects.filter(tender=tender, purpose=FundRequestPurpose.NEST_APPLICATION_FEE)
            .order_by("-requested_at", "-id")
        )
        approved = next((r for r in requests if r.status == FS.APPROVED), None)
        if approved:
            return FS.APPROVED, (
                f"A request for {approved.amount} {approved.currency} was approved by the Managing Director "
                "and is waiting for Finance to pay it and record the payment reference."
            )
        pending = next((r for r in requests if r.status == FS.PENDING), None)
        if pending:
            return FS.PENDING, (
                f"A request for {pending.amount} {pending.currency} is waiting for the Managing Director's approval."
            )
        if requests:  # only declined ones
            return "none", ("The last NeST application-fee request was declined. The PM must raise a new one "
                            "if the bid is to go ahead.")
        return "none", "No NeST application-fee request has been raised. The PM must raise one."
