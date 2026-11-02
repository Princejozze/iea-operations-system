"""
What the dashboards and the nav badges count. One definition each, so the number on a
badge and the length of the list it leads to can never disagree.

dashboards is app 12 (§1): it may import from every app above it, and has no models.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalKind,
    EligibilityDecision,
    EligibilityReview,
    FundRequest,
    FundRequestStatus,
)
from audits.models import QCAudit, Verdict
from core.models import AuditAction, AuditEntry
from items.models import RFQ, Quote
from items.summary import bid_summary, blocker_counts
from tenders.models import Tender, TenderStatus as S
from tenders.views import blocking_gate, days_to_deadline, visible_tenders, with_item_counts

# A tender whose deadline is this close, with items still unpriced, is "at risk" on the
# MD's dashboard. The cron escalation ladder starts at T-3 (§10); the dashboard looks a
# little further ahead so the MD sees it coming before the emails start.
AT_RISK_DAYS = 7
# How far back "returned to me" looks for declined fund requests and eligibility rulings.
RETURNED_LOOKBACK_DAYS = 30

OPEN_PM_STATUSES = (
    S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL, S.DRAFTING, S.COSTING,
    S.PENDING_ELIGIBILITY_DECISION, S.PENDING_AUDIT, S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT,
)
PRICING_STATUSES = (S.DRAFTING, S.COSTING)
# R9: a tender that came back to COSTING from one of these was rejected.
R9_FROM = {S.PENDING_AUDIT: "QC audit", S.PENDING_EXECUTIVE_APPROVAL: "MD"}


# ---------------------------------------------------------------------------
# Counts, for the nav badges (cheap: one COUNT each)
# ---------------------------------------------------------------------------


def md_counts():
    """Everything waiting on the MD. Each is a queue only the MD can clear."""
    return {
        "registrations": Tender.objects.filter(status=S.PENDING_REGISTRATION_APPROVAL).count(),
        "submissions": Tender.objects.filter(status=S.PENDING_EXECUTIVE_APPROVAL).count(),
        "eligibility": EligibilityReview.objects.filter(md_decision="").count(),
        "funds": FundRequest.objects.filter(status=FundRequestStatus.PENDING).count(),
    }


def audit_waiting_count():
    return Tender.objects.filter(status=S.PENDING_AUDIT).count()


def finance_to_pay_count():
    return FundRequest.objects.filter(status=FundRequestStatus.APPROVED).count()


def pm_counts(user):
    mine = Tender.objects.filter(project_manager=user)
    return {
        "to_verify": mine.filter(status=S.PENDING_VERIFICATION).count(),
        "returned": len(returned_to_pm(user)),
    }


# ---------------------------------------------------------------------------
# The MD's dashboard
# ---------------------------------------------------------------------------


def _with_days(tenders):
    for t in tenders:
        t.days_left = days_to_deadline(t)
    return tenders


def signature_queue(user):
    """Audited bids waiting for the MD's submission signature (R3), most urgent first."""
    tenders = _with_days(list(
        visible_tenders(user).filter(status=S.PENDING_EXECUTIVE_APPROVAL)
        .order_by("submission_deadline", "created_at")
    ))
    for t in tenders:
        t.summary = bid_summary(t)
    return tenders


def pending_registrations(user):
    return _with_days(list(
        visible_tenders(user).filter(status=S.PENDING_REGISTRATION_APPROVAL)
        .order_by("submission_deadline", "created_at")
    ))


def open_eligibility_reviews():
    reviews = list(
        EligibilityReview.objects.filter(md_decision="")
        .select_related("tender", "tender__client", "raised_by").order_by("created_at")
    )
    for r in reviews:
        r.days_left = days_to_deadline(r.tender)
    return reviews


def pending_fund_requests():
    funds = list(
        FundRequest.objects.filter(status=FundRequestStatus.PENDING)
        .select_related("tender", "tender__client", "requested_by").order_by("requested_at")
    )
    for fr in funds:
        fr.days_left = days_to_deadline(fr.tender)
    return funds


def tenders_at_risk(user, *, within_days=AT_RISK_DAYS):
    """Tenders still being priced whose deadline is within ``within_days`` and that still
    have unpriced items (or none at all). The PM's move, not the MD's: shown, not brass."""
    now = timezone.now()
    start_of_today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = start_of_today + timedelta(days=within_days + 1)
    tenders = with_item_counts(
        visible_tenders(user).filter(
            status__in=PRICING_STATUSES,
            submission_deadline__gte=start_of_today, submission_deadline__lt=horizon,
        )
    ).order_by("submission_deadline", "id")
    at_risk = []
    for t in tenders:
        t.items_unpriced = t.items_total - t.items_priced
        if t.items_total == 0 or t.items_unpriced > 0:
            t.days_left = days_to_deadline(t)
            at_risk.append(t)
    return at_risk


# ---------------------------------------------------------------------------
# The PM's dashboard
# ---------------------------------------------------------------------------


def pm_open_tenders(user):
    """This PM's tenders that are not yet submitted or closed, nearest deadline first
    (no deadline last), each with its pricing progress and blocking gate."""
    tenders = list(
        with_item_counts(visible_tenders(user).filter(project_manager=user, status__in=OPEN_PM_STATUSES))
        .order_by("submission_deadline", "created_at", "id")
    )
    tenders.sort(key=lambda t: (t.submission_deadline is None, t.submission_deadline or timezone.now()))
    for t in tenders:
        t.days_left = days_to_deadline(t)
        t.items_unpriced = t.items_total - t.items_priced
        t.percent_priced = int(100 * t.items_priced / t.items_total) if t.items_total else 0
        t.gate = blocking_gate(t, items_total=t.items_total, items_priced=t.items_priced)
    return tenders


def nearest_deadline(tenders):
    """The hero: the first open tender whose deadline is today or later, with what is
    blocking it in plain words. None if nothing has a live deadline."""
    for t in tenders:
        if t.days_left is not None and t.days_left >= 0:
            t.blockers = blocker_counts(t) if t.status in PRICING_STATUSES else []
            return t
    return None


@dataclass
class OutstandingRFQ:
    rfq: RFQ
    waiting_items: int
    days_since_sent: int


def quotes_outstanding(user):
    """RFQs this PM's tenders sent where the supplier has not yet quoted on every item asked
    about. Oldest first: the ones most in need of a chase (§10 twice-weekly protocol)."""
    rfqs = list(
        RFQ.objects.filter(tender__project_manager=user, tender__status=S.COSTING)
        .select_related("tender", "supplier").prefetch_related("items").order_by("sent_at", "id")
    )
    if not rfqs:
        return []
    quoted = set(
        Quote.objects.filter(item__tender__in={r.tender_id for r in rfqs}).values_list("item_id", "supplier_id")
    )
    today = timezone.localdate()
    outstanding = []
    for rfq in rfqs:
        waiting = sum(1 for item in rfq.items.all() if (item.pk, rfq.supplier_id) not in quoted)
        if waiting:
            outstanding.append(OutstandingRFQ(rfq, waiting, (today - timezone.localtime(rfq.sent_at).date()).days))
    return outstanding


@dataclass
class Returned:
    tender: object
    what: str  # "Rejected at QC audit", "Fund request declined", ...
    reason: str
    when: object
    url_name: str
    url_arg: int


def _last_status_changes(tenders):
    """{tender pk: its latest STATUS_CHANGE AuditEntry}, in ONE query (PostgreSQL DISTINCT ON):
    this runs on every page, for the PM's nav badge."""
    ids = [str(t.pk) for t in tenders]
    if not ids:
        return {}
    entries = (
        AuditEntry.objects.filter(model="tenders.Tender", object_id__in=ids, action=AuditAction.STATUS_CHANGE)
        .order_by("object_id", "-timestamp", "-id").distinct("object_id")
    )
    return {int(e.object_id): e for e in entries}


def returned_to_pm(user):
    """Everything sent back to this PM that still needs them: R9 rejections (a negative QC
    verdict or a declined submission) on tenders still in COSTING, recently declined fund
    requests with no newer request of the same purpose, and recent REMEDIATE rulings."""
    since = timezone.now() - timedelta(days=RETURNED_LOOKBACK_DAYS)
    returned = []

    costing = list(visible_tenders(user).filter(project_manager=user, status=S.COSTING))
    last_changes = _last_status_changes(costing)
    for t in costing:
        entry = last_changes.get(t.pk)
        came_from = (entry.changes.get("status") or [None])[0] if entry else None
        if came_from == S.PENDING_AUDIT:
            audit = QCAudit.objects.filter(tender=t, verdict=Verdict.NEGATIVE).order_by("-completed_at").first()
            returned.append(Returned(t, "Rejected at QC audit", audit.reason if audit else "", entry.timestamp,
                                     "audits:detail", t.pk))
        elif came_from == S.PENDING_EXECUTIVE_APPROVAL:
            approval = (
                Approval.objects.filter(tender=t, kind=ApprovalKind.SUBMISSION, decision=ApprovalDecision.DECLINED)
                .order_by("-decided_at").first()
            )
            returned.append(Returned(t, "Submission declined by the MD", approval.comments if approval else "",
                                     entry.timestamp, "items:sheet", t.pk))

    declined = (
        FundRequest.objects.filter(tender__project_manager=user, status=FundRequestStatus.DECLINED, decided_at__gte=since)
        .exclude(tender__status__in=(S.SUBMITTED, S.WON, S.LOST, S.WITHDRAWN))
        .select_related("tender", "tender__client")
    )
    for fr in declined:
        newer = FundRequest.objects.filter(tender=fr.tender, purpose=fr.purpose, requested_at__gt=fr.requested_at)
        if not newer.exists():
            returned.append(Returned(fr.tender, f"{fr.get_purpose_display()} declined", fr.decision_reason,
                                     fr.decided_at, "approvals:fund_detail", fr.pk))

    remediate = (
        EligibilityReview.objects.filter(
            tender__project_manager=user, md_decision=EligibilityDecision.REMEDIATE, decided_at__gte=since,
        ).filter(Q(tender__status__in=PRICING_STATUSES)).select_related("tender", "tender__client")
    )
    for r in remediate:
        returned.append(Returned(r.tender, "Eligibility: remediate", r.decision_notes or r.requirement,
                                 r.decided_at, "tenders:detail", r.tender_id))

    returned.sort(key=lambda r: r.when, reverse=True)
    return returned

