"""
Tender screens: list and search (R18), upload (§5 step 2), detail with progress
(§10 "Progress visibility"), and PM verification of the extraction (R12).

Every status change goes through a Tender transition method (R19); these views
only call them and report refusals. Items are reached through the reverse
accessor ``tender.items`` — never an import from apps below (§1).
"""

from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.models import Role

from .forms import TenderUploadForm, TenderVerificationForm
from .models import Tender, TenderStatus as S

PAGE_SIZE = 50
POLL_SECONDS = 10  # gentle on 3G; extraction takes one to two minutes (§6)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _require_tender_reader(user):
    """R5: clearing agents see post-award records only — never tenders."""
    if user.role == Role.CLEARING_AGENT:
        raise PermissionDenied("Clearing agents have access to post-award records only.")


def visible_tenders(user):
    """The tender queryset a user may read. Enforced here, not in templates (R5)."""
    _require_tender_reader(user)
    return Tender.objects.select_related("client", "project_manager")


def search_tenders(queryset, q):
    """R18: system reference, legacy reference, official number, client name and title."""
    q = " ".join((q or "").split())
    if not q:
        return queryset
    return queryset.filter(
        Q(reference__icontains=q)
        | Q(legacy_reference__icontains=q)
        | Q(official_reference__icontains=q)
        | Q(client__name__icontains=q)
        | Q(client__short_name__icontains=q)
        | Q(title__icontains=q)
    )


# ---------------------------------------------------------------------------
# Progress: stage, items priced, days to deadline, blocking gate
# ---------------------------------------------------------------------------


STAGE_ORDER = (
    S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL, S.DRAFTING, S.COSTING,
    S.PENDING_AUDIT, S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT, S.SUBMITTED,
)
# Statuses where the next move is the MD's — the only use of brass in the UI.
NEEDS_MD_STATUSES = (S.PENDING_REGISTRATION_APPROVAL, S.PENDING_EXECUTIVE_APPROVAL, S.PENDING_ELIGIBILITY_DECISION)
CLOSED_STATUSES = (S.WON, S.LOST, S.WITHDRAWN)


@dataclass
class Gate:
    text: str
    who: str = ""  # "MD", "PM", "Auditor", "Finance", "System"
    details: tuple = ()

    @property
    def needs_md(self):
        return self.who == "MD"


def blocking_gate(tender, *, items_total=None, items_priced=None):
    """What is stopping this tender moving on, and whose move it is."""
    st = tender.status
    if st == S.EXTRACTING:
        return Gate("Reading the tender PDF — usually one to two minutes.", "System")
    if st == S.PENDING_VERIFICATION:
        return Gate("Extracted fields need the PM's verification (R12).", "PM")
    if st == S.PENDING_REGISTRATION_APPROVAL:
        return Gate("Awaiting the MD's registration approval (R15).", "MD")
    if st == S.PENDING_ELIGIBILITY_DECISION:
        return Gate("Frozen: awaiting the MD's eligibility decision (R9a, R16).", "MD")
    if st == S.DRAFTING:
        if not items_total:
            return Gate("No items yet — add items before costing can start.", "PM")
        return Gate("Ready for the PM to start costing.", "PM")
    if st == S.COSTING:
        blockers = tender.r2_blockers()
        if blockers:
            unpriced = (items_total or 0) - (items_priced or 0)
            head = f"{unpriced} item(s) unpriced." if unpriced else "Items incomplete for audit."
            return Gate(f"{head} QC audit is blocked (R2).", "PM", tuple(blockers))
        return Gate("Complete — ready for the PM to submit to QC audit.", "PM")
    if st == S.PENDING_AUDIT:
        return Gate("Awaiting the QC audit verdict.", "Auditor")
    if st == S.PENDING_EXECUTIVE_APPROVAL:
        return Gate("Awaiting the MD's submission approval (R3).", "MD")
    if st == S.READY_TO_SUBMIT:
        if not tender.has_nest_funds:
            return Gate("The NeST account is not funded — no paid application-fee request (R17).", "Finance")
        return Gate("Funded — ready for the PM to submit on NeST.", "PM")
    if st == S.SUBMITTED:
        return Gate("Awaiting the award outcome.", "PM")
    return Gate("")  # WON, LOST, WITHDRAWN: nothing blocks a closed tender


def days_to_deadline(tender):
    if tender.submission_deadline is None:
        return None
    return (timezone.localtime(tender.submission_deadline).date() - timezone.localdate()).days


def with_item_counts(queryset):
    return queryset.annotate(
        items_total=Count("items", distinct=True),
        items_priced=Count("items", filter=Q(items__unit_cost__isnull=False), distinct=True),
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@login_required
def tender_list(request):
    q = request.GET.get("q", "")
    # Meta.ordering is dropped once Count() adds a GROUP BY, so order explicitly.
    tenders = with_item_counts(search_tenders(visible_tenders(request.user), q)).order_by("-created_at", "-id")
    page = Paginator(tenders, PAGE_SIZE).get_page(request.GET.get("page"))
    for t in page:
        t.days_left = days_to_deadline(t)
        t.needs_md = t.status in NEEDS_MD_STATUSES
    context = {"page": page, "q": q, "can_upload": request.user.role == Role.PROJECT_MANAGER}
    if request.headers.get("HX-Request"):
        return render(request, "tenders/_tender_rows.html", context)
    return render(request, "tenders/tender_list.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def tender_upload(request):
    if request.user.role != Role.PROJECT_MANAGER:
        raise PermissionDenied("Only a Project Manager can register a tender by upload.")
    form = TenderUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            tender = Tender.objects.create_from_upload(uploaded_by=request.user, pdf=form.cleaned_data["pdf"])
        except ValidationError as exc:
            form.add_error("pdf", exc)
        else:
            # §6: nothing else happens in the request; the cron command extracts it.
            messages.success(request, f"{tender.reference} created. Reading the PDF takes about one to two minutes.")
            return redirect("tenders:detail", pk=tender.pk)
    return render(request, "tenders/tender_upload.html", {"form": form})


def _detail_context(request, tender):
    counts = with_item_counts(Tender.objects.filter(pk=tender.pk)).values("items_total", "items_priced").get()
    gate = blocking_gate(tender, **counts)
    stage_index = STAGE_ORDER.index(tender.status) if tender.status in STAGE_ORDER else None
    return {
        "tender": tender,
        "items_total": counts["items_total"],
        "items_priced": counts["items_priced"],
        "days_left": days_to_deadline(tender),
        "gate": gate,
        "stages": [
            {"label": S(s).label, "done": stage_index is not None and i < stage_index,
             "current": s == tender.status}
            for i, s in enumerate(STAGE_ORDER)
        ],
        "closed": tender.status in CLOSED_STATUSES,
        "poll_seconds": POLL_SECONDS,
        "is_pm": tender.project_manager_id == request.user.pk,
        "is_md": request.user.role == Role.EXECUTIVE,
        # RelatedObjectDoesNotExist is an AttributeError, so getattr's default applies.
        "extraction": getattr(tender, "extraction", None),
    }


@login_required
def tender_detail(request, pk):
    tender = get_object_or_404(visible_tenders(request.user), pk=pk)
    return render(request, "tenders/tender_detail.html", _detail_context(request, tender))


@login_required
def tender_status(request, pk):
    """Polled by htmx while EXTRACTING. Once the tender has moved on, the page reloads."""
    tender = get_object_or_404(visible_tenders(request.user), pk=pk)
    if tender.status != S.EXTRACTING:
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    return render(request, "tenders/_extracting.html", {"tender": tender, "poll_seconds": POLL_SECONDS})


@login_required
@require_http_methods(["GET", "POST"])
def tender_verify(request, pk):
    """§5 step 7: the PM checks every extracted field against its source page and confirms (R12)."""
    tender = get_object_or_404(visible_tenders(request.user), pk=pk)
    if tender.project_manager_id != request.user.pk:
        raise PermissionDenied("Only this tender's PM can verify it.")
    if tender.status != S.PENDING_VERIFICATION:
        messages.info(request, f"{tender.reference} is {tender.get_status_display().lower()}; there is nothing to verify.")
        return redirect("tenders:detail", pk=tender.pk)

    extraction = getattr(tender, "extraction", None)
    extracted = extraction.fields if extraction else {}
    form = TenderVerificationForm(request.POST or None, tender=tender, extracted=extracted)

    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                client = form.resolve_client()
                tender.confirm_extraction(request.user, **form.verified_values(client))
        except ValidationError as exc:
            _add_transition_errors(form, exc)
        else:
            messages.success(request, f"{tender.reference} confirmed and sent to the MD for registration approval.")
            return redirect("tenders:detail", pk=tender.pk)

    rows = []
    for name in form.FIELD_ORDER:
        key = form.extraction_key(name)
        record = extracted.get(key) if key else None
        draft_value = (record or {}).get("value")
        rows.append({
            "field": form[name],
            "record": record,
            # False is a real answer (site visit not required); only None, "" and [] mean not found.
            "found": draft_value not in (None, "", []),
            "is_list": isinstance(draft_value, list),
            "date_only": isinstance(draft_value, str) and len(draft_value.strip()) == 10
            and name.endswith(("deadline", "date")),
            "required": name in Tender.REQUIRED_AT_CONFIRMATION,
        })
    return render(request, "tenders/tender_verify.html", {
        "tender": tender, "form": form, "rows": rows, "extraction": extraction,
    })


def _add_transition_errors(form, exc):
    """Put a refused confirmation's errors on the matching fields (or the form)."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            target = name if name in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for message in exc.messages:
            form.add_error(None, message)
