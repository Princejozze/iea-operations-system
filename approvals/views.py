"""
The MD's signed decisions — registration approval (§5 step 8, R15) and submission
approval (R3, R9) — and fund requests: raise, approve or decline, mark paid (R17).

A signed decision is recorded only through Approval.objects.sign, which verifies the
signature and calls the tender's transition method in the same transaction (R19).
Fund requests move only through FundRequest.approve / decline / mark_paid.
Eligibility reviews (R16): the PM raises one with EligibilityReview.objects.raise_for,
the MD rules with EligibilityReview.decide; both call the tender's transition methods.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from accounts.models import Role
from tenders.models import Tender, TenderStatus as S

from items.forms import add_model_errors
from items.summary import bid_summary
from tenders.views import days_to_deadline, visible_tenders

from .forms import (
    EligibilityDecisionForm, FundDecisionForm, FundRequestForm, MarkPaidForm, RaiseEligibilityForm,
    RegistrationDecisionForm, SubmissionDecisionForm,
)
from .models import (
    FUND_REQUEST_TENDER_STATUSES, Approval, ApprovalDecision, ApprovalKind, EligibilityDecision, EligibilityReview,
    FundRequest, FundRequestPurpose, FundRequestStatus,
)


def _require_md(user):
    if user.role != Role.EXECUTIVE:
        raise PermissionDenied("Only the Managing Director decides registration approvals (R15).")


@login_required
def registration_queue(request):
    _require_md(request.user)
    tenders = (
        Tender.objects.filter(status=S.PENDING_REGISTRATION_APPROVAL)
        .select_related("client", "project_manager")
        .order_by("submission_deadline", "created_at")
    )
    return render(request, "approvals/registration_queue.html", {"tenders": tenders})


@login_required
@require_http_methods(["GET", "POST"])
def registration_decide(request, pk):
    _require_md(request.user)
    tender = get_object_or_404(Tender.objects.select_related("client", "project_manager", "category"), pk=pk)
    if tender.status != S.PENDING_REGISTRATION_APPROVAL:
        messages.info(request, f"{tender.reference} is not awaiting registration approval.")
        return redirect("tenders:detail", pk=tender.pk)

    form = RegistrationDecisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            Approval.objects.sign(
                tender, ApprovalKind.REGISTRATION, d["decision"], request.user,
                typed_name=d["typed_name"], password=d["password"], comments=d["comments"],
            )
        except PermissionDenied as exc:  # signature did not verify
            form.add_error(None, str(exc))
        except ValidationError as exc:  # the tender refused the transition
            for message in exc.messages:
                form.add_error(None, message)
        else:
            if d["decision"] == ApprovalDecision.APPROVED:
                messages.success(request, f"{tender.reference} registered. The PM has been told work may begin.")
            else:
                messages.success(request, f"{tender.reference} declined and withdrawn. The PM has been told why.")
            return redirect("approvals:registration_queue")
    return render(request, "approvals/registration_decide.html", {"tender": tender, "form": form})


# ---------------------------------------------------------------------------
# Submission approval (R3, R9)
# ---------------------------------------------------------------------------


def _require_md_for(user, what):
    if user.role != Role.EXECUTIVE:
        raise PermissionDenied(f"Only the Managing Director decides {what}.")


@login_required
def submission_queue(request):
    _require_md_for(request.user, "submission approvals")
    tenders = list(
        Tender.objects.filter(status=S.PENDING_EXECUTIVE_APPROVAL)
        .select_related("client", "project_manager").order_by("submission_deadline", "created_at")
    )
    for t in tenders:
        t.days_left = days_to_deadline(t)
        t.summary = bid_summary(t)
    return render(request, "approvals/submission_queue.html", {"tenders": tenders})


def _submission_context(tender):
    """Everything the MD signs for, on one screen: the money, the audit, the funds, the rates."""
    audit = tender.qc_audits.exclude(verdict="").select_related("auditor").order_by("-completed_at").first()
    lines = list(audit.lines.all()) if audit else []
    funds = list(FundRequest.objects.filter(tender=tender).select_related("requested_by").order_by("-requested_at"))
    return {
        "tender": tender, "summary": bid_summary(tender), "audit": audit,
        "audit_lines": len(lines), "audit_flagged": sum(1 for ln in lines if ln.variance),
        "funds": funds, "has_nest_funds": tender.has_nest_funds, "days_left": days_to_deadline(tender),
    }


@login_required
@require_http_methods(["GET", "POST"])
def submission_decide(request, pk):
    _require_md_for(request.user, "submission approvals")
    tender = get_object_or_404(Tender.objects.select_related("client", "project_manager", "category"), pk=pk)
    if tender.status != S.PENDING_EXECUTIVE_APPROVAL:
        messages.info(request, f"{tender.reference} is not awaiting submission approval.")
        return redirect("tenders:detail", pk=tender.pk)
    form = SubmissionDecisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            Approval.objects.sign(
                tender, ApprovalKind.SUBMISSION, d["decision"], request.user,
                typed_name=d["typed_name"], password=d["password"], comments=d["comments"],
            )
        except PermissionDenied as exc:  # signature did not verify
            form.add_error(None, str(exc))
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
        else:
            if d["decision"] == ApprovalDecision.APPROVED:
                messages.success(request, f"{tender.reference} approved for submission. It now waits only on the NeST account being funded.")
            else:
                messages.success(request, f"{tender.reference} declined and returned to costing. The PM has been told why.")
            return redirect("approvals:submission_queue")
    return render(request, "approvals/submission_decide.html", {**_submission_context(tender), "form": form})


# ---------------------------------------------------------------------------
# Fund requests (§7; R17)
# ---------------------------------------------------------------------------

FUND_READERS = (Role.EXECUTIVE, Role.FINANCE, Role.HEAD_OF_OPERATIONS, Role.PROJECT_MANAGER)


def _visible_funds(user):
    """Scoped in the queryset, not the template: a PM sees requests on their own tenders only."""
    if user.role not in FUND_READERS:
        raise PermissionDenied("Fund requests are for the PM, the MD, Finance and the Head of Operations.")
    qs = FundRequest.objects.select_related("tender", "requested_by", "tender__client")
    if user.role == Role.PROJECT_MANAGER:
        qs = qs.filter(tender__project_manager=user)
    return qs


@login_required
def fund_list(request):
    qs = _visible_funds(request.user)
    me = request.user.role
    open_now = list(qs.filter(status__in=[FundRequestStatus.PENDING, FundRequestStatus.APPROVED]))
    for fr in open_now:
        fr.needs_md = me == Role.EXECUTIVE and fr.status == FundRequestStatus.PENDING
        fr.needs_you = fr.needs_md or (me == Role.FINANCE and fr.status == FundRequestStatus.APPROVED)
    open_now.sort(key=lambda fr: (not fr.needs_you, fr.requested_at))
    recent = list(qs.filter(status__in=[FundRequestStatus.PAID, FundRequestStatus.DECLINED]).order_by("-requested_at")[:20])
    return render(request, "approvals/fund_list.html", {"open_requests": open_now, "recent": recent})


@login_required
@require_http_methods(["GET", "POST"])
def fund_new(request, tender_pk):
    tender = get_object_or_404(visible_tenders(request.user), pk=tender_pk)
    if request.user.role != Role.PROJECT_MANAGER or tender.project_manager_id != request.user.pk:
        raise PermissionDenied("Only this tender's PM raises its fund requests.")
    form = FundRequestForm(request.POST or None)
    allowed = tender.status in FUND_REQUEST_TENDER_STATUSES
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        fr = FundRequest(tender=tender, purpose=d["purpose"], amount=d["amount"], currency=d["currency"],
                         notes=d["notes"], requested_by=request.user)
        try:
            fr.save()
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, ValidationError):
                add_model_errors(form, exc)
            else:
                form.add_error(None, str(exc))
        else:
            messages.success(request, "Fund request sent to the Managing Director.")
            return redirect("approvals:fund_detail", pk=fr.pk)
    return render(request, "approvals/fund_new.html", {
        "tender": tender, "form": form, "allowed": allowed, "has_nest_funds": tender.has_nest_funds,
        "existing": list(FundRequest.objects.filter(tender=tender).order_by("-requested_at")),
    })


@login_required
def fund_detail(request, pk):
    fr = get_object_or_404(_visible_funds(request.user), pk=pk)
    role, status = request.user.role, fr.status
    return render(request, "approvals/fund_detail.html", {
        "fr": fr, "tender": fr.tender, "has_nest_funds": fr.tender.has_nest_funds,
        "decision_form": FundDecisionForm() if role == Role.EXECUTIVE and status == FundRequestStatus.PENDING else None,
        "paid_form": MarkPaidForm() if role == Role.FINANCE and status == FundRequestStatus.APPROVED else None,
        "is_nest_fee": fr.purpose == FundRequestPurpose.NEST_APPLICATION_FEE,
    })


@login_required
@require_http_methods(["POST"])
def fund_decide(request, pk):
    fr = get_object_or_404(_visible_funds(request.user), pk=pk)
    form = FundDecisionForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect("approvals:fund_detail", pk=fr.pk)
    d = form.cleaned_data
    try:
        if d["decision"] == "APPROVE":
            fr.approve(request.user)
            messages.success(request, "Approved. Finance has been told to pay it.")
        else:
            fr.decline(request.user, d["reason"])
            messages.success(request, "Declined. The PM has been told why.")
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
    return redirect("approvals:fund_detail", pk=fr.pk)


@login_required
@require_http_methods(["POST"])
def fund_pay(request, pk):
    fr = get_object_or_404(_visible_funds(request.user), pk=pk)
    form = MarkPaidForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter the payment reference.")
        return redirect("approvals:fund_detail", pk=fr.pk)
    try:
        fr.mark_paid(request.user, form.cleaned_data["payment_reference"])
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
    else:
        funded = " The NeST account is now funded for this tender." if fr.purpose == FundRequestPurpose.NEST_APPLICATION_FEE else ""
        messages.success(request, "Marked paid. The PM has been told." + funded)
    return redirect("approvals:fund_detail", pk=fr.pk)


# ---------------------------------------------------------------------------
# Eligibility reviews (R16, R9a)
# ---------------------------------------------------------------------------

RAISE_FROM = (S.DRAFTING, S.COSTING)

DECISION_DONE = {
    EligibilityDecision.PROCEED: "Proceed. {ref} is unfrozen and back in {status}.",
    EligibilityDecision.JOINT_VENTURE: "Joint venture with {partner}. {ref} is unfrozen and back in {status}.",
    EligibilityDecision.REMEDIATE: "Remediate. {ref} is unfrozen and back in {status}; the PM has your notes.",
    EligibilityDecision.WITHDRAW: "{ref} is withdrawn. The PM has been told why.",
}


def _form_errors_from(form, exc):
    """A model refusal onto the form: on the matching field where there is one."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            for error in errors:
                form.add_error(name if name in form.fields else None, error)
    else:
        for message in getattr(exc, "messages", [str(exc)]):
            form.add_error(None, message)


@login_required
def eligibility_queue(request):
    _require_md_for(request.user, "eligibility reviews (R16)")
    reviews = list(
        EligibilityReview.objects.filter(md_decision="")
        .select_related("tender", "tender__client", "raised_by").order_by("created_at")
    )
    for r in reviews:
        r.days_left = days_to_deadline(r.tender)
    return render(request, "approvals/eligibility_queue.html", {"reviews": reviews})


@login_required
@require_http_methods(["GET", "POST"])
def eligibility_raise(request, tender_pk):
    """R16: the tender's PM escalates an unmet special requirement. Raising it freezes the
    tender (R9a) until the MD rules."""
    tender = get_object_or_404(visible_tenders(request.user), pk=tender_pk)
    if tender.project_manager_id != request.user.pk:
        raise PermissionDenied("Only this tender's PM can raise an eligibility review (R16).")
    open_review = tender.eligibility_reviews.filter(md_decision="").first()
    if open_review:
        messages.info(request, "This tender already has an open eligibility review.")
        return redirect("approvals:eligibility_review", pk=open_review.pk)
    if tender.status not in RAISE_FROM:
        messages.info(request, f"An eligibility review can be raised only while the tender is drafting or costing; "
                               f"{tender.reference} is {tender.get_status_display().lower()}.")
        return redirect("tenders:detail", pk=tender.pk)

    form = RaiseEligibilityForm(request.POST or None, tender=tender)
    if request.method == "POST" and form.is_valid():
        try:
            review = EligibilityReview.objects.raise_for(
                tender, request.user, requirement=form.cleaned_data["requirement"],
                pm_notes=form.cleaned_data["pm_notes"],
            )
        except (ValidationError, PermissionDenied) as exc:
            _form_errors_from(form, exc)
        else:
            messages.success(request, f"Sent to the MD. {tender.reference} is frozen until the ruling (R9a).")
            return redirect("approvals:eligibility_review", pk=review.pk)
    return render(request, "approvals/eligibility_raise.html", {"tender": tender, "form": form})


@login_required
@require_http_methods(["GET", "POST"])
def eligibility_review(request, pk):
    """One review. While it is open the MD rules on it here; afterwards, and for everyone
    else who may read the tender, it is the record of what was asked and decided."""
    review = get_object_or_404(
        EligibilityReview.objects.select_related("tender", "tender__client", "raised_by", "decided_by", "jv_partner"),
        pk=pk,
    )
    tender = get_object_or_404(visible_tenders(request.user), pk=review.tender_id)  # R5 and 403 for CLEAR
    is_md = request.user.role == Role.EXECUTIVE
    can_decide = is_md and review.is_open and tender.status == S.PENDING_ELIGIBILITY_DECISION

    if request.method == "POST" and not can_decide:
        if not is_md:
            raise PermissionDenied("Only the Managing Director rules on an eligibility review (R16).")
        messages.info(request, "This review has already been decided.")
        return redirect("approvals:eligibility_review", pk=review.pk)

    form = EligibilityDecisionForm(request.POST or None) if can_decide else None
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            with transaction.atomic():  # a refused ruling leaves no new supplier behind
                partner = form.resolve_partner()
                review.decide(request.user, d["md_decision"], notes=d["decision_notes"], jv_partner=partner)
        except (ValidationError, PermissionDenied) as exc:
            _form_errors_from(form, exc)
        else:
            tender.refresh_from_db()
            messages.success(request, DECISION_DONE[d["md_decision"]].format(
                ref=tender.reference, status=tender.get_status_display().lower(), partner=partner,
            ))
            return redirect("tenders:detail", pk=tender.pk)

    return render(request, "approvals/eligibility_review.html", {
        "review": review, "tender": tender, "form": form, "can_decide": can_decide, "is_md": is_md,
        "is_pm": tender.project_manager_id == request.user.pk,
        "back_to": S(tender.previous_status).label.lower() if tender.previous_status else None,
        "days_left": days_to_deadline(tender),
        "history": tender.eligibility_reviews.exclude(pk=review.pk).select_related("decided_by", "jv_partner"),
    })
