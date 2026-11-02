"""
Quote entry, §5 step 13: "Attach the supplier's original ... Enter the figures
... The typed figures are the working data; the attachment is the proof. Keep both."

``enter_quote`` is the one way a quote is created. It refuses a quote without
its document and a document without its figures, validates the figures before
anything is written to storage, and stores the document as a QUOTATION
attachment of the item — so the same file also satisfies R2's attachment check.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import audit_actor

from ..models import AttachmentKind, Incoterm, ItemAttachment, Quote, _is_tenders_pm


def _filename(document):
    return getattr(document, "name", "") or ""


def has_document(document):
    """A real, non-empty file. (A ContentFile is truthy even with no name and no bytes.)"""
    if not document or not _filename(document):
        return False
    try:
        return document.size > 0
    except (AttributeError, OSError, TypeError):
        return False


def build_quote(item, *, supplier, user, quoted_unit_price, currency, incoterm=Incoterm.CIF,
                shipping_applies=True, country_of_origin="", make_brand="", model_part_number="",
                specification_offered="", lead_time_days=None, valid_until=None, notes="", received_at=None):
    """An unsaved Quote with its figures checked, minus the document. Raises
    ValidationError with every problem at once."""
    quote = Quote(
        item=item, supplier=supplier, entered_by=user,
        quoted_unit_price=quoted_unit_price, currency=currency or "", incoterm=incoterm or Incoterm.CIF,
        shipping_applies=shipping_applies, country_of_origin=country_of_origin or "",
        make_brand=make_brand or "", model_part_number=model_part_number or "",
        specification_offered=specification_offered or "", lead_time_days=lead_time_days,
        valid_until=valid_until, notes=notes or "", received_at=received_at or timezone.now(),
    )
    errors = quote._invariant_errors()
    errors.pop("original_document", None)  # attached by the caller, after this check
    if not _is_tenders_pm(user, item.tender):
        errors["entered_by"] = "Only this tender's Project Manager enters its quotes."
    if errors:
        raise ValidationError(errors)
    return quote


def save_with_document(quote, document, *, user, stored_name=None, original_filename=None):
    """
    Store ``document`` as the item's QUOTATION attachment and save ``quote`` with
    it, in one transaction. ``stored_name`` reuses a file already in storage (the
    xlsx import stores one supplier quotation once and points every row at it).
    """
    with transaction.atomic(), audit_actor(user):
        attachment = ItemAttachment(
            item=quote.item, kind=AttachmentKind.QUOTATION, uploaded_by=user,
            original_filename=(original_filename or _filename(document))[:255],
        )
        if stored_name:
            attachment.file.name = stored_name
        else:
            attachment.file = document
        attachment.save()
        quote.original_document = attachment
        quote.save()
    return quote


def enter_quote(item, *, supplier, original_document, user, **figures):
    """
    Record what ``supplier`` offered for ``item``: ``original_document`` (an
    uploaded file — the quotation exactly as sent) plus the figures as keyword
    arguments (see ``build_quote``). Both are mandatory. Returns the RECEIVED quote;
    selecting it is a separate decision (``Quote.select``).
    """
    if not has_document(original_document):
        raise ValidationError({"original_document": "Attach the supplier's quotation exactly as they sent it."})
    quote = build_quote(item, supplier=supplier, user=user, **figures)
    return save_with_document(quote, original_document, user=user)
