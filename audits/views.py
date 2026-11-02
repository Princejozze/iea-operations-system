"""
The QC audit screen (CLAUDE.md §3 "QC audit", R1, R9).

Per item line: the specification the tender REQUIRES beside the specification the
supplier OFFERED, the numeric differences flagged between them, and the auditor's
verdict. The system flags and never judges (§14): "required 656 kg, offered 620 kg,
-36 kg" is shown, and what it means is the auditor's decision.

Each line saves over htmx the way the item sheet does: one form per line, answered
with out-of-band fragments (the recomputed variance, a status line, the audit's
progress), so the auditor reading 101 lines is never reloaded onto a new page.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import Role
from items.forms import add_model_errors, error_messages
from tenders.models import Tender, TenderStatus as S
from tenders.views import days_to_deadline, visible_tenders, with_item_counts

from .forms import AuditLineForm, CompleteAuditForm
from .models import QCAudit, QCAuditLine, Verdict
from .variance import describe

AUDIT_READERS = (Role.EXECUTIVE, Role.HEAD_OF_OPERATIONS, Role.AUDITOR)
FILTERS = (("all", "All"), ("undecided", "Undecided"), ("flagged", "With variance"), ("negative", "Do not comply"))


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _can_read(user, tender):
    if user.role in AUDIT_READERS:
        return True
    return user.role == Role.PROJECT_MANAGER and tender.project_manager_id == user.pk


def _tender_for_reading(user, pk):
    tender = get_object_or_404(visible_tenders(user), pk=pk)  # R5: clearing agents never get here
    if not _can_read(user, tender):
        raise PermissionDenied("This audit is not yours to read.")
    return tender


def _can_edit(user, audit):
    """Only the audit's own auditor, while it is open (R1: never the tender's PM)."""
    return user.role == Role.AUDITOR and audit.auditor_id == user.pk and not audit.is_complete


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@login_required
def queue(request):
    if request.user.role not in AUDIT_READERS:
        raise PermissionDenied("The audit queue is for auditors and the management team.")
    waiting = list(
        with_item_counts(Tender.objects.filter(status=S.PENDING_AUDIT).select_related("client", "project_manager"))
        .order_by("submission_deadline", "created_at")
    )
    for t in waiting:
        t.days_left = days_to_deadline(t)
        t.open_audit = QCAudit.objects.filter(tender=t, verdict="").select_related("auditor").first()
        t.refusal = request.user.audit_refusal_reason(t) if request.user.role == Role.AUDITOR else ""
    return render(request, "audits/queue.html", {"waiting": waiting})


# ---------------------------------------------------------------------------
# One tender's audit
# ---------------------------------------------------------------------------


def _line_flags(line):
    """Variance as (label, sentence, kind) — straight from the model's own wording (audits.variance)."""
    return [
        {"label": f["dimension"].capitalize(), "kind": f["type"], "text": describe(f).split(": ", 1)[1]}
        for f in line.variance
    ]


def _decorate(lines):
    for line in lines:
        line.flags = _line_flags(line)
    return lines


def _summary_context(audit):
    lines = list(audit.lines.all())
    decided = [ln for ln in lines if ln.verdict]
    return {
        "audit": audit, "total": len(lines), "decided": len(decided),
        "negative": sum(1 for ln in decided if ln.verdict == Verdict.NEGATIVE),
        "flagged": sum(1 for ln in lines if ln.variance),
        "undecided": len(lines) - len(decided),
        "percent": int(100 * len(decided) / len(lines)) if lines else 0,
    }


@login_required
def audit_detail(request, tender_pk):
    tender = _tender_for_reading(request.user, tender_pk)
    audit = tender.qc_audits.select_related("auditor").order_by("-created_at", "-id").first()
    refusal = ""
    can_start = (
        audit is None or audit.is_complete
    ) and tender.status == S.PENDING_AUDIT and request.user.role == Role.AUDITOR
    if can_start:
        refusal = request.user.audit_refusal_reason(tender) or ""
    context = {
        "tender": tender, "audit": audit, "can_start": can_start and not refusal, "refusal": refusal,
        "past_audits": list(
            tender.qc_audits.exclude(verdict="").exclude(pk=audit.pk if audit else None).select_related("auditor")
        ),
    }
    if audit is None:
        return render(request, "audits/audit.html", context)

    show = request.GET.get("show", "all")
    lines = _decorate(list(audit.lines.select_related("item", "item__supplier").order_by("item__item_reference")))
    summary = _summary_context(audit)
    shown = {
        "undecided": [ln for ln in lines if not ln.verdict],
        "flagged": [ln for ln in lines if ln.variance],
        "negative": [ln for ln in lines if ln.verdict == Verdict.NEGATIVE],
    }.get(show, lines)
    can_edit = _can_edit(request.user, audit)
    if can_edit:
        for ln in shown:
            ln.form = AuditLineForm(instance=ln, initial={"verdict": ln.verdict})
    context.update(summary)
    context.update({
        "lines": shown, "show": show if show in dict(FILTERS) else "all", "can_edit": can_edit,
        "filters": [(k, label, _count(k, lines)) for k, label in FILTERS],
        "complete_form": CompleteAuditForm() if can_edit else None,
    })
    return render(request, "audits/audit.html", context)


def _count(key, lines):
    return {
        "undecided": sum(1 for ln in lines if not ln.verdict),
        "flagged": sum(1 for ln in lines if ln.variance),
        "negative": sum(1 for ln in lines if ln.verdict == Verdict.NEGATIVE),
    }.get(key, len(lines))


@login_required
@require_POST
def start(request, tender_pk):
    tender = get_object_or_404(visible_tenders(request.user), pk=tender_pk)
    if request.user.role != Role.AUDITOR:
        raise PermissionDenied("Only an auditor opens a QC audit.")
    try:
        audit = QCAudit.objects.start(tender, request.user)  # prefills required AND offered (§5 step 13)
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return redirect("audits:detail", tender_pk=tender.pk)
    messages.success(request, f"Audit opened with {audit.lines.count()} lines. "
                              "Offered specifications are filled from the selected quotes where there is one.")
    return redirect("audits:detail", tender_pk=tender.pk)


# ---------------------------------------------------------------------------
# Saving a line (htmx) and completing the audit
# ---------------------------------------------------------------------------


def _line_response(request, line, *, state="Saved", errors=(), status=200):
    line = QCAuditLine.objects.select_related("item", "audit").get(pk=line.pk)
    line.flags = _line_flags(line)
    return render(request, "audits/_line_saved.html", {
        **_summary_context(line.audit), "tender": line.audit.tender, "line": line, "state": state, "errors": list(errors), "oob": True,
        "can_edit": _can_edit(request.user, line.audit), "complete_form": CompleteAuditForm(),
    }, status=status)


@login_required
@require_POST
def line_save(request, pk):
    line = get_object_or_404(QCAuditLine.objects.select_related("audit__tender", "item"), pk=pk)
    audit = line.audit
    if not _can_edit(request.user, audit):
        raise PermissionDenied("Only this audit's own auditor can change it, and not once it is complete.")
    data = request.POST.copy()
    note = ""
    if data.get("verdict") == Verdict.NEGATIVE and not (data.get("comments") or "").strip():
        # Mid-entry, not a mistake: they have ticked "does not comply" and the reason is next.
        # Keep the verdict it had; the offered text they typed is still saved.
        data["verdict"] = line.verdict
        note = "Add a comment saying why, and “Does not comply” is recorded."
    form = AuditLineForm(data, instance=line)
    if form.is_valid():
        try:
            form.instance.save()
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, ValidationError):
                add_model_errors(form, exc)
            else:
                form.add_error(None, str(exc))
    if form.errors:
        return _line_response(request, line, state="", status=422,
                              errors=["Not saved. " + "; ".join(error_messages(form))])
    return _line_response(request, line, state=note or "Saved")


@login_required
@require_POST
def complete(request, tender_pk):
    tender = get_object_or_404(visible_tenders(request.user), pk=tender_pk)
    audit = tender.qc_audits.filter(verdict="").first()
    if audit is None:
        messages.info(request, "There is no open audit to complete.")
        return redirect("audits:detail", tender_pk=tender.pk)
    form = CompleteAuditForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        audit.complete(request.user, reason=reason)
    except (ValidationError, PermissionDenied) as exc:
        text = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
        undecided = [m for m in text if m.endswith("no verdict yet.")]
        if undecided:
            messages.error(request, f"{len(undecided)} line{'s' if len(undecided) != 1 else ''} still "
                                    "need a verdict before the audit can be completed.")
        else:
            messages.error(request, "; ".join(text))
        return redirect("audits:detail", tender_pk=tender.pk)
    audit.refresh_from_db()
    if audit.verdict == Verdict.NEGATIVE:
        messages.warning(request, f"{tender.reference}: negative audit recorded. The tender is back in costing "
                                  "with exchange rates unlocked, and the PM has been told why.")
    else:
        messages.success(request, f"{tender.reference}: audit positive. The tender goes to the MD for submission approval.")
    return redirect("audits:detail", tender_pk=tender.pk)
