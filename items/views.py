"""
Item sheet, quote entry, attachments (CLAUDE.md §5 steps 11-13, §7).

The item sheet is the hardest screen: tenders run to 101 items. Each item is a card
holding one form that saves on ``change`` over htmx and answers with small
out-of-band fragments (its computed figures, its status line, the sheet totals), so
the PM edits a field and moves on — no page reload, and no re-render of the form
the PM is typing in. It works at 375px because it is cards, not a table.

Every write goes through the model layer, which is the authority on R2, R4, R9a,
R10 and R15; these views only call it and report what it refuses. Money is never
posted: the PM enters a supplier's unit price and the costing chain computes the rest.
"""

import mimetypes

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import ProtectedError
from django.http import FileResponse, Http404, HttpResponse
from django.utils import timezone
from django.utils.html import escape
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.models import Role
from suppliers.models import Supplier
from tenders.models import WORK_ALLOWED_STATUSES, Tender, TenderStatus as S
from tenders.views import visible_tenders

from .forms import (
    AttachmentForm, ItemRowForm, NewItemForm, QuoteForm, QuoteSheetUploadForm,
    add_model_errors, error_messages,
)
from .models import AttachmentKind, Item, ItemAttachment, Quote, QuoteStatus
from .services.quote_sheet import build_quote_sheet, import_quote_sheet
from .services.quotes import enter_quote
from .summary import bid_summary, blocker_counts, missing_for_audit

# Who may read an item sheet. Finance sees budget figures on fund requests, not item
# costs; clearing agents see post-award records only (R5, enforced in visible_tenders).
SHEET_READERS = (Role.EXECUTIVE, Role.HEAD_OF_OPERATIONS, Role.PROJECT_MANAGER, Role.AUDITOR)
FILTERS = (("all", "All"), ("unpriced", "Unpriced"), ("incomplete", "Incomplete"))


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _tender_for_reading(user, pk):
    if user.role not in SHEET_READERS:
        raise PermissionDenied("The item sheet is for the PM, the Head of Operations, the auditor and the MD.")
    return get_object_or_404(visible_tenders(user), pk=pk)


def _can_edit(user, tender):
    """Only the tender's own PM, and only while item work is allowed (R15, R4)."""
    return (
        user.role == Role.PROJECT_MANAGER and tender.project_manager_id == user.pk
        and tender.status in WORK_ALLOWED_STATUSES
    )


def _tender_for_editing(user, tender):
    if user.role != Role.PROJECT_MANAGER or tender.project_manager_id != user.pk:
        raise PermissionDenied("Only this tender's PM edits its items.")
    return tender


def _item_for(user, pk, *, edit):
    item = get_object_or_404(Item.objects.select_related("tender", "supplier", "selected_quote"), pk=pk)
    if edit:
        _tender_for_editing(user, item.tender)
    else:
        _tender_for_reading(user, item.tender_id)
    return item


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------


def _decorate(items):
    """Attach what a card shows: what R2 still needs, the attachment kinds held, the quote count."""
    for item in items:
        kinds = {a.kind for a in item.attachments.all()}
        item.held_kinds = kinds
        item.missing = missing_for_audit(item, kinds)
        item.quote_count = len(item.quotes.all())
        item.is_priced = item.unit_cost is not None
        item.kinds = AttachmentKind.choices
        item.first_missing_kind = next((k for k, _ in AttachmentKind.choices if k not in kinds), "")
    return items


def _load_items(tender):
    return _decorate(list(
        tender.items.select_related("supplier", "selected_quote")
        .prefetch_related("attachments", "quotes").order_by("item_reference")
    ))


def _summary_context(tender, items=None):
    items = _load_items(tender) if items is None else items
    return {
        "tender": tender, "summary": bid_summary(tender),
        "not_ready": sum(1 for i in items if i.missing),
    }


@login_required
def sheet(request, pk):
    tender = _tender_for_reading(request.user, pk)
    items = _load_items(tender)
    show = request.GET.get("show", "all")
    shown = {
        "unpriced": [i for i in items if not i.is_priced],
        "incomplete": [i for i in items if i.missing],
    }.get(show, items)
    can_edit = _can_edit(request.user, tender)
    if can_edit:
        for item in shown:
            item.row_form = ItemRowForm(instance=item)
    return render(request, "items/sheet.html", {
        **_summary_context(tender, items),
        "items": shown, "show": show if show in dict(FILTERS) else "all",
        "filters": [(key, label, _filter_count(key, items)) for key, label in FILTERS],
        "can_edit": can_edit,
        "is_pm": request.user.role == Role.PROJECT_MANAGER and tender.project_manager_id == request.user.pk,
        "new_item_form": NewItemForm() if can_edit else None,
        "supplier_names": list(Supplier.objects.order_by("name").values_list("name", flat=True)) if can_edit else [],
        "can_start_costing": _is_pm_of(request.user, tender) and tender.status == S.DRAFTING,
        "can_submit_audit": _is_pm_of(request.user, tender) and tender.status == S.COSTING,
    })


def _is_pm_of(user, tender):
    return user.role == Role.PROJECT_MANAGER and tender.project_manager_id == user.pk


def _filter_count(key, items):
    if key == "unpriced":
        return sum(1 for i in items if not i.is_priced)
    if key == "incomplete":
        return sum(1 for i in items if i.missing)
    return len(items)


def _card_context(request, item, tender, form=None):
    return {"item": item, "tender": tender, "can_edit": _can_edit(request.user, tender),
            "form": form or ItemRowForm(instance=item)}


def _saved_fragments(request, item, tender, *, state="Saved", errors=(), status=200):
    """The out-of-band answer to one edit: figures, status line, and the sheet totals."""
    _decorate([item])
    context = {
        **_summary_context(tender), "item": item, "oob": True, "state": state, "errors": list(errors),
        "can_edit": _can_edit(request.user, tender),
    }
    return render(request, "items/_item_saved.html", context, status=status)


@login_required
@require_POST
def item_save(request, pk):
    item = _item_for(request.user, pk, edit=True)
    tender = item.tender
    if not _can_edit(request.user, tender):
        return _saved_fragments(request, _fresh(item), tender, state="", status=422,
                                errors=[f"Not saved: the tender is {tender.get_status_display().lower()}, so items are read-only."])
    if (item.selected_quote_id is None and request.POST.get("unit_price", "").strip()
            and not request.POST.get("currency")):
        # Mid-entry, not a mistake: the price is typed and the currency is next. Nothing is saved
        # until both are there (the model refuses a price without its currency), and the PM is told
        # so in plain words rather than shown a red error on every price they type.
        return _saved_fragments(request, _fresh(item), tender, state="Choose the currency to save this price.")
    form = ItemRowForm(request.POST, instance=item)
    if form.is_valid():
        try:
            form.instance.save()
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, ValidationError):
                add_model_errors(form, exc)
            else:
                form.add_error(None, str(exc))
    if form.errors:
        return _saved_fragments(request, _fresh(item), tender, state="", status=422,
                                errors=["Not saved. " + "; ".join(error_messages(form))])
    return _saved_fragments(request, _fresh(item), tender)


@login_required
def item_more(request, pk):
    """The rarely-edited fields of one card, fetched when the PM first opens "More"."""
    item = _item_for(request.user, pk, edit=True)
    if not _can_edit(request.user, item.tender):
        raise PermissionDenied("This item is read-only.")
    return render(request, "items/_item_more.html", {"item": item, "form": ItemRowForm(instance=item)})


def _fresh(item):
    return Item.objects.select_related("tender", "supplier", "selected_quote").prefetch_related(
        "attachments", "quotes").get(pk=item.pk)


@login_required
@require_POST
def item_add(request, pk):
    tender = get_object_or_404(visible_tenders(request.user), pk=pk)
    _tender_for_editing(request.user, tender)
    form = NewItemForm(request.POST)
    if not form.is_valid() or not _can_edit(request.user, tender):
        text = "; ".join(error_messages(form)) or f"The tender is {tender.get_status_display().lower()}: items are read-only."
        return _add_error(text)
    d = form.cleaned_data
    item = Item(tender=tender, specification=d["specification"], quantity=d["quantity"], unit_of_measure=d["unit_of_measure"])
    try:
        item.save()
    except (ValidationError, PermissionDenied) as exc:
        return _add_error("; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
    item = _fresh(item)
    _decorate([item])
    return render(request, "items/_item_added.html", {
        **_card_context(request, item, tender), **_summary_context(tender), "oob": True,
        "new_item_form": NewItemForm(), "just_saved": True,
    })


def _add_error(text):
    """A refused add: shown in the form's own error slot, never appended to the item list."""
    response = HttpResponse(f'<div class="small flag" role="alert">{escape(text)}</div>', status=422)
    response["HX-Retarget"] = "#new-item-error"
    response["HX-Reswap"] = "innerHTML"
    return response


@login_required
@require_POST
def item_delete(request, pk):
    item = _item_for(request.user, pk, edit=True)
    tender = item.tender
    try:
        item.delete()
    except ProtectedError:
        return _saved_fragments(request, _fresh(item), tender, state="", status=422,
                                errors=["Not deleted: it has quotes or audit lines. Reject its quotes first, or leave it."])
    except (ValidationError, PermissionDenied) as exc:
        return _saved_fragments(request, _fresh(item), tender, state="", status=422,
                                errors=["Not deleted. " + ("; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))])
    return render(request, "items/_item_gone.html", {**_summary_context(tender), "oob": True})


# ---------------------------------------------------------------------------
# Attachments (the three R2 kinds)
# ---------------------------------------------------------------------------


@login_required
@require_POST
def attachment_add(request, pk):
    item = _item_for(request.user, pk, edit=True)
    form = AttachmentForm(request.POST, request.FILES)
    errors = []
    if not _can_edit(request.user, item.tender):
        errors = [f"The tender is {item.tender.get_status_display().lower()}: items are read-only."]
    elif form.is_valid():
        upload = form.cleaned_data["file"]
        try:
            ItemAttachment.objects.create(
                item=item, kind=form.cleaned_data["kind"], file=upload,
                original_filename=upload.name[:255], uploaded_by=request.user,
            )
        except (ValidationError, PermissionDenied) as exc:
            errors = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
    else:
        errors = error_messages(form)
    return _attachments_response(request, item, errors)


@login_required
@require_POST
def attachment_delete(request, pk):
    attachment = get_object_or_404(ItemAttachment.objects.select_related("item__tender"), pk=pk)
    item = _item_for(request.user, attachment.item_id, edit=True)
    errors = []
    if hasattr(attachment, "quote"):
        errors = ["This is the original document of a quote, so it stays with the quote."]
    else:
        try:
            attachment.delete()
        except (ValidationError, PermissionDenied, ProtectedError) as exc:
            errors = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
    return _attachments_response(request, item, errors)


def _attachments_response(request, item, errors):
    item = _fresh(item)
    _decorate([item])
    tender = item.tender
    return render(request, "items/_attachments_saved.html", {
        **_summary_context(tender), "item": item, "tender": tender, "oob": True,
        "attach_form": AttachmentForm(), "can_edit": _can_edit(request.user, tender),
        "errors": errors, "attach_errors": errors, "state": "" if errors else "Saved",
    }, status=422 if errors else 200)


@login_required
def attachment_download(request, pk):
    """Authenticated, so a document is never reachable by guessing a storage URL (R5)."""
    attachment = get_object_or_404(ItemAttachment.objects.select_related("item__tender"), pk=pk)
    _tender_for_reading(request.user, attachment.item.tender_id)
    try:
        handle = attachment.file.open("rb")
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise Http404("The file is not in storage.") from exc
    name = attachment.original_filename or attachment.file.name.rsplit("/", 1)[-1]
    response = FileResponse(handle, content_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
    response["Content-Disposition"] = f'inline; filename="{name.replace(chr(34), "")}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# Moving the tender along: start costing, submit for audit (R19: transition methods only)
# ---------------------------------------------------------------------------


@login_required
@require_POST
def start_costing(request, pk):
    tender = get_object_or_404(visible_tenders(request.user), pk=pk)
    try:
        tender.start_costing(request.user)
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
    else:
        messages.success(request, f"{tender.reference} is now in costing. RFQs and quotes can go ahead.")
    return redirect("items:sheet", pk=tender.pk)


@login_required
@require_POST
def submit_for_audit(request, pk):
    tender = get_object_or_404(visible_tenders(request.user), pk=pk)
    try:
        tender.submit_for_audit(request.user)
    except (ValidationError, PermissionDenied) as exc:
        blockers = blocker_counts(tender)
        if blockers:
            tender_items = tender.items.count()
            text = ("Not ready for QC audit yet: "
                    + ", ".join(f"{n} of {tender_items} need {label}" for label, n in blockers) + ".")
        else:
            text = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        messages.error(request, text)
    else:
        messages.success(request, f"{tender.reference} is with the QC auditors. Exchange rates are now locked.")
    return redirect("items:sheet", pk=tender.pk)


# ---------------------------------------------------------------------------
# Quotes (§5 step 13)
# ---------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def quotes(request, pk):
    item = _item_for(request.user, pk, edit=False)
    tender = item.tender
    can_edit = _can_edit(request.user, tender)
    form = QuoteForm(request.POST or None, request.FILES or None, item=item)
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied("Quotes are entered by the tender's PM, while the tender is in drafting or costing.")
        if form.is_valid():
            try:
                supplier = form.resolve_supplier()
                quote = enter_quote(
                    item, supplier=supplier, original_document=form.cleaned_data["original_document"],
                    user=request.user, **form.figures(),
                )
            except (ValidationError, PermissionDenied) as exc:
                if isinstance(exc, ValidationError):
                    add_model_errors(form, exc)
                else:
                    form.add_error(None, str(exc))
            else:
                if form.cleaned_data["select_now"]:
                    try:
                        quote.select(request.user)
                    except (ValidationError, PermissionDenied) as exc:
                        messages.warning(request, "Quote saved, but not selected: " + (
                            "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)))
                    else:
                        messages.success(request, "Quote saved and selected. The item is re-costed on it.")
                else:
                    messages.success(request, "Quote saved. Select it when you are ready to cost the item on it.")
                return redirect("items:quotes", pk=item.pk)
    quote_list = list(item.quotes.select_related("supplier", "original_document").order_by("quoted_unit_price", "id"))
    return render(request, "items/quotes.html", {
        "item": item, "tender": tender, "quotes": quote_list, "form": form, "can_edit": can_edit,
        "has_selected": any(q.status == QuoteStatus.SELECTED for q in quote_list),
        "today": timezone.localdate(),
    })


def _quote_action(request, pk, action):
    quote = get_object_or_404(Quote.objects.select_related("item__tender"), pk=pk)
    item = quote.item
    _tender_for_editing(request.user, item.tender)
    try:
        getattr(quote, action)(request.user)
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
    else:
        if action == "select":
            messages.success(request, f"{item.item_reference} is now costed on the quote from {quote.supplier}.")
        else:
            messages.success(request, "Quote set aside.")
    return redirect("items:quotes", pk=item.pk)


@login_required
@require_POST
def quote_select(request, pk):
    return _quote_action(request, pk, "select")


@login_required
@require_POST
def quote_reject(request, pk):
    return _quote_action(request, pk, "reject")


# ---------------------------------------------------------------------------
# xlsx round-trip for quote entry at scale (§7 "Entry at scale")
# ---------------------------------------------------------------------------


@login_required
def quote_sheet_download(request, pk):
    tender = _tender_for_reading(request.user, pk)
    _tender_for_editing(request.user, tender)
    response = HttpResponse(
        build_quote_sheet(tender),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{tender.reference}-quote-sheet.xlsx"'
    return response


@login_required
@require_http_methods(["GET", "POST"])
def quote_sheet_upload(request, pk):
    tender = _tender_for_reading(request.user, pk)
    _tender_for_editing(request.user, tender)
    form = QuoteSheetUploadForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        try:
            result = import_quote_sheet(
                tender, supplier=form.cleaned_data["supplier"], sheet=form.cleaned_data["sheet"],
                original_document=form.cleaned_data["original_document"], user=request.user,
            )
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        else:
            if result.ok:
                messages.success(request, f"{len(result.quotes)} quote(s) imported"
                                 + (f"; {result.skipped} row(s) had no quote and were skipped." if result.skipped else "."))
                return redirect("items:sheet", pk=tender.pk)
    return render(request, "items/quote_sheet_upload.html", {"tender": tender, "form": form, "result": result})
