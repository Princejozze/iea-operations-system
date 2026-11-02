"""
Tender screens: list and search (R18), upload (§5 step 2), detail with progress
(§10 "Progress visibility"), and PM verification of the extraction (R12).

Every status change goes through a Tender transition method (R19); these views
only call them and report refusals. Items are reached through the reverse
accessor ``tender.items`` — never an import from apps below (§1).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.models import Role

from .forms import RATE_FIELD_INFO, CostingRatesForm, ReassignForm, TenderUploadForm, TenderVerificationForm
from .models import RATES_EDITABLE_STATUSES, Tender, TenderCostingRates, TenderStatus as S

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
    """The tender queryset a user may read. Enforced here, in the queryset, never in
    templates: clearing agents get none (R5); a PM gets only the tenders they hold now
    (R23); the MD, Head of Operations, Auditor, Finance and Admin read every tender.

    Scoping on the current ``project_manager`` is what makes R13 reassignment transfer
    access: the new PM gains the tender and the previous PM loses it, at once."""
    _require_tender_reader(user)
    tenders = Tender.objects.select_related("client", "project_manager")
    if user.role == Role.PROJECT_MANAGER:
        tenders = tenders.filter(project_manager=user)
    return tenders


def tender_or_403(user, pk):
    """The one tender ``pk`` that ``user`` may read, from visible_tenders(). Every view that
    reaches a tender, or anything under it (items, quotes, attachments, audits, funds),
    starts here.

    404 when no such tender exists; 403 when it exists but is not theirs (R23): a PM asking
    for another PM's tender is refused, not told it is missing, and never merely shown a
    page without the link."""
    try:
        return visible_tenders(user).get(pk=pk)
    except Tender.DoesNotExist:
        if Tender.objects.filter(pk=pk).exists():
            raise PermissionDenied("This tender is held by another Project Manager (R23).") from None
        raise Http404("No such tender.") from None


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
            head = (f"{unpriced} of {items_total} items have no price yet." if unpriced
                    else "Some items still need details or documents.")
            return Gate(f"{head} The QC audit cannot start until the item sheet is complete.", "PM", tuple(blockers))
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
        # R22: every reader may see the rates; before registration there is nothing to price.
        "show_rates_link": tender.status not in (S.EXTRACTING, S.PENDING_VERIFICATION),
        # R16 via the reverse accessor — approvals sits below tenders (§1).
        "eligibility_reviews": (list(tender.eligibility_reviews.select_related("jv_partner"))
                                if hasattr(tender, "eligibility_reviews") else []),
        # R13: who has held it, oldest first. Every reader sees it; only the MD may change it.
        "assignments": _assignment_history(tender),
        "can_raise_eligibility": tender.project_manager_id == request.user.pk and tender.status in (S.DRAFTING, S.COSTING),
        # RelatedObjectDoesNotExist is an AttributeError, so getattr's default applies.
        "extraction": getattr(tender, "extraction", None),
        # Reverse accessors, never imports (§1): items, audits and approvals live below this app.
        # The PM already has an "Open the item sheet" button in the gate card while drafting or costing.
        "gate_has_sheet_button": tender.project_manager_id == request.user.pk and tender.status in (S.DRAFTING, S.COSTING),
        "can_read_sheet": request.user.role in (Role.EXECUTIVE, Role.HEAD_OF_OPERATIONS, Role.PROJECT_MANAGER, Role.AUDITOR),
        "can_read_audit": request.user.role in (Role.EXECUTIVE, Role.HEAD_OF_OPERATIONS, Role.AUDITOR)
        or (request.user.role == Role.PROJECT_MANAGER and tender.project_manager_id == request.user.pk),
        "has_audit": tender.qc_audits.exists() if hasattr(tender, "qc_audits") else False,
        "can_request_funds": tender.status in (S.DRAFTING, S.COSTING, S.PENDING_ELIGIBILITY_DECISION, S.PENDING_AUDIT,
                                               S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT),
        "fund_requests": (list(tender.fund_requests.all())
                          if hasattr(tender, "fund_requests") and request.user.role in (
                              Role.EXECUTIVE, Role.FINANCE, Role.HEAD_OF_OPERATIONS)
                          or (hasattr(tender, "fund_requests") and tender.project_manager_id == request.user.pk) else []),
    }


@login_required
def tender_detail(request, pk):
    tender = tender_or_403(request.user, pk)
    return render(request, "tenders/tender_detail.html", _detail_context(request, tender))


@login_required
def tender_status(request, pk):
    """Polled by htmx while EXTRACTING. Once the tender has moved on, the page reloads."""
    tender = tender_or_403(request.user, pk)
    if tender.status != S.EXTRACTING:
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    return render(request, "tenders/_extracting.html", {"tender": tender, "poll_seconds": POLL_SECONDS})


@login_required
@require_http_methods(["GET", "POST"])
def tender_verify(request, pk):
    """§5 step 7: the PM checks every extracted field against its source page and confirms (R12)."""
    tender = tender_or_403(request.user, pk)
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


# ---------------------------------------------------------------------------
# The MD's pricing screen (R22)
# ---------------------------------------------------------------------------
#
# TenderCostingRates.preview() and .apply() do all the work and all the checking;
# these views only collect numbers and show results. preview() saves nothing, so
# the MD may try numbers on a locked tender too; apply() refuses unless EXEC and
# DRAFTING or COSTING, whatever this screen shows.


def rates_lock_reason(status):
    """Why a tender's rates cannot be changed now, in plain words. None if they can."""
    if status in RATES_EDITABLE_STATUSES:
        return None
    if status == S.PENDING_ELIGIBILITY_DECISION:
        return ("Frozen while the eligibility review is decided (R9a). They unlock when the "
                "tender returns to drafting or costing.")
    if status in (S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL):
        return "Not registered yet. The rates can be set once the tender is approved and drafting (R15)."
    if status in (S.PENDING_AUDIT, S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT):
        return ("Locked when the tender left costing for QC audit, together with the exchange rates "
                "(R4, R22). A failed audit or a declined submission returns it to costing and unlocks them (R9).")
    if status == S.WITHDRAWN:
        return "The tender is withdrawn."
    return "The bid has been submitted. These are the rates it was priced at."


def _tzs(value):
    """Whole TZS with thousands separators: 5,398,400."""
    return f"{Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP):,}"


def _rate_text(value):
    """20.00 -> 20, 22.25 -> 22.25, 15000.00 -> 15,000."""
    return f"{Decimal(value).normalize():,f}"


def _comparison(baseline, candidate):
    """Baseline vs candidate bid totals (BidPreview each), ready for the template."""
    diff = candidate.total_selling - baseline.total_selling
    if baseline.total_selling:
        pct = (diff / baseline.total_selling * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pct_text = f"{pct:+.2f}%".replace("-", "−")
    else:
        pct_text = None  # nothing priced yet: a percentage of zero means nothing
    sign = "+" if diff > 0 else ("−" if diff < 0 else "")
    return {
        "baseline": _tzs(baseline.total_selling),
        "candidate": _tzs(candidate.total_selling),
        "diff": f"{sign}{_tzs(abs(diff))}",
        "diff_pct": pct_text,
        "direction": "up" if diff > 0 else ("down" if diff < 0 else "same"),
        "priced": candidate.priced_items,
        "unpriced": candidate.unpriced_items,
        "items": candidate.priced_items + candidate.unpriced_items,
    }


def _changes(saved, new):
    """One row per rate the candidate changes: label, old, new, unit."""
    return [
        {"label": RATE_FIELD_INFO[name][0], "old": _rate_text(saved[name]),
         "new": _rate_text(new[name]), "unit": RATE_FIELD_INFO[name][2]}
        for name in TenderCostingRates.RATE_FIELDS
        if Decimal(new[name]) != Decimal(saved[name])
    ]


def _rates_tender(request, pk):
    tender = tender_or_403(request.user, pk)
    return tender, TenderCostingRates.for_tender(tender)


def _preview_or_errors(row, form):
    """(comparison, changes) for a bound form, or (None, None) with the errors on the form."""
    if not form.is_valid():
        return None, None
    try:
        candidate = row.preview(**form.cleaned_data)
    except ValidationError as exc:
        _add_transition_errors(form, exc)
        return None, None
    return _comparison(row.preview(), candidate), _changes(row.values(), form.cleaned_data)


@login_required
def tender_rates(request, pk):
    """
    The MD tunes this bid's rates (R22). Every tender reader sees the rates; only EXEC
    gets inputs. With htmx each change re-previews (a GET: nothing is saved); without
    JavaScript the "Preview" button submits the same GET and the page re-renders.
    """
    tender, row = _rates_tender(request, pk)
    is_md = request.user.role == Role.EXECUTIVE
    is_htmx = bool(request.headers.get("HX-Request"))
    if is_htmx and not is_md:
        raise PermissionDenied("Only the MD tries costing rates (R22).")
    saved = row.values()
    lock_reason = rates_lock_reason(tender.status)

    trying = is_md and any(name in request.GET for name in TenderCostingRates.RATE_FIELDS)
    # Inputs show 10 and 15000, not 10.00 and 15000.00.
    form = CostingRatesForm(request.GET if trying else None,
                            initial={n: f"{Decimal(v).normalize():f}" for n, v in saved.items()})
    if trying:
        comparison, changes = _preview_or_errors(row, form)
    else:
        current = row.preview()
        comparison, changes = _comparison(current, current), []

    defaults = TenderCostingRates.baseline_values()
    context = {
        "tender": tender, "form": form, "is_md": is_md, "lock_reason": lock_reason,
        "can_apply": is_md and lock_reason is None,
        "comparison": comparison, "changes": changes,
        "rows": [
            {"field": form[name], "label": label, "how": how, "unit": unit,
             "saved": _rate_text(saved[name]), "default": _rate_text(defaults[name]),
             "is_default": Decimal(saved[name]) == Decimal(defaults[name])}
            for name, (label, how, unit) in RATE_FIELD_INFO.items()
        ],
    }
    template = "tenders/_rates_result.html" if is_htmx else "tenders/tender_rates.html"
    return render(request, template, context)


@login_required
@require_http_methods(["POST"])
def tender_rates_apply(request, pk):
    """
    Two steps, because applying changes the bid price: the first POST shows exactly what
    will change and the new total; only the second (``confirm=yes``) calls apply().
    """
    tender, row = _rates_tender(request, pk)
    if request.user.role != Role.EXECUTIVE:
        raise PermissionDenied("Only the MD may set a tender's costing rates (R22).")
    back = reverse("tenders:rates", args=[tender.pk])
    lock_reason = rates_lock_reason(tender.status)
    if lock_reason:
        messages.error(request, f"The rates were not changed. {lock_reason}")
        return redirect(back)

    form = CostingRatesForm(request.POST)
    comparison, changes = _preview_or_errors(row, form)
    if comparison is None:
        problems = "; ".join(
            f"{form.fields[name].label}: {errors[0]}" if name in form.fields else errors[0]
            for name, errors in form.errors.items()
        )
        messages.error(request, f"Nothing was applied. {problems}")
        return redirect(back)
    if not changes:
        messages.info(request, "Those are already this tender's rates; there is nothing to apply.")
        return redirect(back)

    if request.POST.get("confirm") != "yes":
        return render(request, "tenders/tender_rates_confirm.html", {
            "tender": tender, "form": form, "comparison": comparison, "changes": changes, "is_md": True,
            "back": f"{back}?{urlencode({n: form.cleaned_data[n] for n in TenderCostingRates.RATE_FIELDS})}",
        })

    try:
        result = row.apply(request.user, **form.cleaned_data)
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "The rates were not changed. " + " ".join(getattr(exc, "messages", [str(exc)])))
        return redirect(back)
    plural = "" if result.items_recosted == 1 else "s"
    messages.success(
        request,
        f"Rates applied. Bid total {_tzs(result.old_total)} → {_tzs(result.new_total)} TZS ex VAT; "
        f"{result.items_recosted} item{plural} re-costed.",
    )
    return redirect(back)


# ---------------------------------------------------------------------------
# Reassignment (R13)
# ---------------------------------------------------------------------------
#
# Tender.reassign() does the checking (EXEC only, an active PM, not the current holder)
# and the work (TenderAssignment row, audit entry, both PMs emailed). No status gate:
# a tender needs an owner at every status, submitted and closed ones included.


def _assignment_history(tender):
    return list(tender.assignments.select_related("from_pm", "to_pm", "assigned_by").order_by("created_at", "id"))


@login_required
@require_http_methods(["GET", "POST"])
def tender_reassign(request, pk):
    """Pick a PM, then confirm on a page that names both people. Only the confirmed POST
    (``confirm=yes``) calls reassign(), so a mis-tap on a phone moves nothing."""
    tender = tender_or_403(request.user, pk)
    if request.user.role != Role.EXECUTIVE:
        raise PermissionDenied("Only the Managing Director can reassign a tender (R13).")

    form = ReassignForm(request.POST or None, tender=tender)
    context = {"tender": tender, "form": form, "assignments": _assignment_history(tender)}
    if request.method == "POST" and form.is_valid():
        new_pm, reason = form.cleaned_data["new_pm"], form.cleaned_data["reason"].strip()
        if request.POST.get("confirm") != "yes":
            return render(request, "tenders/tender_reassign_confirm.html", {**context, "new_pm": new_pm, "reason": reason})
        try:
            tender.reassign(request.user, new_pm, reason=reason)
        except (ValidationError, PermissionDenied) as exc:
            for message in getattr(exc, "messages", [str(exc)]):
                form.add_error(None, message)
        else:
            messages.success(request, f"{tender.reference} now belongs to {new_pm.get_full_name() or new_pm.email}. "
                                      "Both PMs have been emailed.")
            return redirect("tenders:detail", pk=tender.pk)
    return render(request, "tenders/tender_reassign.html", context)
