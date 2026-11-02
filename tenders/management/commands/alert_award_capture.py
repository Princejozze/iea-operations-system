"""
Award-capture prompt after bid opening (CLAUDE.md §10 "Alerts", §12).

The legacy sheets recorded an award for 6 of 183 submitted tenders, so there
is no win rate to report. This command closes that gap by asking the PM to
record the outcome once the bids have been opened.

The prompt runs from bid opening: ``Tender.bid_opening_date`` when the client
states one, otherwise the submission deadline — under the PPRA standard
tendering documents bids are opened publicly at the deadline (``Tender.opening_at``).

A tender is prompted while it is SUBMITTED, its bids have been opened, and it
has no TenderOutcome or only a PENDING one. The first prompt goes out on the
first run after opening; it then repeats every ``--every`` days (default 7)
until the outcome is recorded, because an award decision can take weeks and a
single reminder is what the old sheets already proved does not work. Never
more than once per tender per day (see ``core.management.alerts``).

The prompt goes to the tender's PM; if the PM's account is inactive, to the
Managing Director, who may also record an outcome (``Tender.record_outcome``).

Cron, daily:

    10 7 * * *  cd /srv/iea-system && .venv/bin/python manage.py alert_award_capture >> /var/log/iea/alerts.log 2>&1
"""

from datetime import timedelta

from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import Role, User
from core.management.alerts import AlertCommand, clean_recipients, last_sent, local_today, start_of_local_day, tender_header
from tenders.models import AwardStatus, Tender, TenderStatus

KIND = "AWARD_CAPTURE"


class Command(AlertCommand):
    help = "Prompt the PM to record position at opening and award result once bids are opened. Run daily."

    def add_arguments(self, parser):
        parser.add_argument("--every", type=int, default=7,
                            help="Days between repeat prompts while the outcome is unrecorded (default 7).")

    def handle_alerts(self, *, every, **options):
        today_start = start_of_local_day(local_today())
        # Repeat only if the last prompt was on or before this local date.
        repeat_cutoff = start_of_local_day(local_today() - timedelta(days=max(every, 1) - 1))
        tenders = (
            Tender.objects.annotate(opens_at=Coalesce("bid_opening_date", "submission_deadline"))
            .filter(status=TenderStatus.SUBMITTED, opens_at__lte=timezone.now())
            .filter(Q(outcome__isnull=True) | Q(outcome__award_status=AwardStatus.PENDING))
            .select_related("project_manager", "client", "outcome")
            .order_by("opens_at")
        )
        for tender in tenders:
            previous = last_sent(KIND, tender)
            if previous is not None and previous >= repeat_cutoff:
                continue
            pm = tender.project_manager
            audience = [pm] if pm.is_active else list(
                User.objects.filter(role=Role.EXECUTIVE, is_active=True).order_by("pk")
            )
            outcome = getattr(tender, "outcome", None)
            position_known = bool(outcome and outcome.bid_opening_position)
            opened = timezone.localdate(tender.opening_at)
            if position_known:
                ask = (f"The position at opening is recorded ({outcome.get_bid_opening_position_display()}), "
                       "but the award result is still pending. Record it as soon as the client announces it.")
            else:
                ask = ("Please record the position at opening (lowest, second lowest, …) and, when known, "
                       "the award result and contract sum.")
            self.deliver(
                kind=KIND, tender=tender, recipients=clean_recipients(audience),
                subject=f"Record the outcome — {tender.reference}",
                body=(
                    f"{tender_header(tender)}\n\n"
                    f"Bids for this tender were opened on {opened:%d %B %Y} and no award result is recorded.\n\n"
                    f"{ask}\n\n"
                    "Without it the company cannot report a win rate.\n"
                    + ("" if previous is None else
                       f"\n(Reminder: last prompted {timezone.localdate(previous):%d %B %Y}.)\n")
                ),
                not_since=today_start,
            )
