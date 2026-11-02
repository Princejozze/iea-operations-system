"""
T-minus escalation ladder for unpriced items (CLAUDE.md §10 "Alerts").

A tender whose submission deadline is close and which still has an item with
no ``unit_cost`` is in danger of missing the bid: R2 will refuse the audit.
The ladder widens the circle each day:

    T-3   the tender's PM
    T-2   the PM and the Head of Operations
    T-1   the whole management team — the Managing Director, the Head of
          Operations and the PM — warning that the bid is in jeopardy

"T-n" counts calendar days in Africa/Dar_es_Salaam: a deadline at 10:00 on
Thursday is T-1 all day Wednesday. Each rung is a separate alert, sent at most
once per tender per day (see ``core.management.alerts``).

Tenders considered: DRAFTING, COSTING and PENDING_ELIGIBILITY_DECISION — the
states in which items can still be unpriced. From PENDING_AUDIT on, R2 has
already proved every item priced.

Cron, daily in the early morning (re-running later the same day is harmless):

    0 7 * * *  cd /srv/iea-system && .venv/bin/python manage.py alert_unpriced_items >> /var/log/iea/alerts.log 2>&1
"""

from django.db.models import Count, Q

from accounts.models import Role, User
from core.management.alerts import AlertCommand, clean_recipients, days_until, local_today, tender_header
from tenders.models import Tender, TenderStatus

S = TenderStatus

OPEN_FOR_PRICING = (S.DRAFTING, S.COSTING, S.PENDING_ELIGIBILITY_DECISION)

KIND = {3: "UNPRICED_T_MINUS_3", 2: "UNPRICED_T_MINUS_2", 1: "UNPRICED_T_MINUS_1"}


def _users(role):
    return list(User.objects.filter(role=role, is_active=True).order_by("pk"))


class Command(AlertCommand):
    help = "T-3 / T-2 / T-1 escalation for tenders with unpriced items. Run daily by cron."

    def handle_alerts(self, **options):
        today = local_today()
        tenders = (
            Tender.objects.filter(status__in=OPEN_FOR_PRICING, submission_deadline__isnull=False)
            .annotate(
                unpriced=Count("items", filter=Q(items__unit_cost__isnull=True)),
                item_count=Count("items"),
            )
            .filter(unpriced__gt=0)
            .select_related("project_manager", "client")
            .order_by("submission_deadline")
        )
        hops = execs = None
        for tender in tenders:
            days = days_until(tender.submission_deadline, today)
            if days not in KIND:
                continue
            if hops is None:
                hops, execs = _users(Role.HEAD_OF_OPERATIONS), _users(Role.EXECUTIVE)
            pm = tender.project_manager
            audience = {3: [pm], 2: [pm, *hops], 1: [*execs, *hops, pm]}[days]
            self.deliver(
                kind=KIND[days], tender=tender, recipients=clean_recipients(audience),
                subject=self._subject(tender, days), body=self._body(tender, days),
            )

    @staticmethod
    def _subject(tender, days):
        n = tender.unpriced
        what = f"{n} unpriced item{'s' if n != 1 else ''}"
        if days == 1:
            return f"BID IN JEOPARDY — {tender.reference}: {what}, deadline tomorrow"
        return f"T-{days}: {tender.reference} has {what}"

    @staticmethod
    def _body(tender, days):
        n, total = tender.unpriced, tender.item_count
        lines = [tender_header(tender), ""]
        lines.append(f"{n} of {total} item{'s' if total != 1 else ''} still {'has' if n == 1 else 'have'} no unit cost.")
        lines.append(f"Status: {tender.get_status_display()}. "
                     f"Project Manager: {tender.project_manager.get_full_name() or tender.project_manager.email}.")
        lines.append("")
        if tender.status == S.PENDING_ELIGIBILITY_DECISION:
            lines.append("The tender is frozen awaiting the Managing Director's eligibility decision (R9a): "
                         "no pricing can happen until it is resolved.")
        if days == 1:
            lines.append("The submission deadline is TOMORROW. The tender cannot go to QC audit while any item is "
                         "unpriced (R2), so an unpriced item now jeopardises the whole bid.")
        elif days == 2:
            lines.append("The submission deadline is in 2 days. Escalated to the Head of Operations.")
        else:
            lines.append("The submission deadline is in 3 days. Every item must be priced before the tender can go "
                         "to QC audit (R2).")
        return "\n".join(lines) + "\n"
