"""
Reference generation — CLAUDE.md §4 "Reference formats" and R20.

    Tender: IEA-{YYYY}-{NNNN}        e.g. IEA-2026-0042   (restarts each year)
    Item:   {tender_reference}-{NNN} e.g. IEA-2026-0042-007

Both functions must be called inside the transaction that creates the record
being numbered:

    with transaction.atomic():
        tender = Tender(reference=next_tender_reference(), ...)
        tender.save()

The counter row is locked with select_for_update() until that transaction
ends, so a concurrent upload waits rather than reading the same value. If the
transaction rolls back, the increment rolls back with it and the number is
reused — sequences have no gaps.
"""

from django.db import transaction
from django.db.transaction import TransactionManagementError
from django.utils import timezone

from .models import ReferenceCounter

TENDER_PREFIX = "IEA"
TENDER_SEQUENCE_MAX = 9999  # {NNNN}
ITEM_SEQUENCE_MAX = 999  # {NNN}


class ReferenceSequenceExhausted(Exception):
    """The sequence would overflow its fixed width. Refuse rather than emit a
    reference that breaks the §4 format."""


def next_tender_reference(*, on_date=None, using="default"):
    """Return the next tender reference for the year of ``on_date``
    (default: today in Africa/Dar_es_Salaam)."""
    year = (on_date or timezone.localdate()).year
    value = _take_next(f"tender:{year}", using)
    if value > TENDER_SEQUENCE_MAX:
        raise ReferenceSequenceExhausted(f"More than {TENDER_SEQUENCE_MAX} tenders in {year}.")
    return f"{TENDER_PREFIX}-{year}-{value:04d}"


def next_item_reference(tender_reference, *, using="default"):
    """Return the next item reference within the given tender."""
    value = _take_next(f"item:{tender_reference}", using)
    if value > ITEM_SEQUENCE_MAX:
        raise ReferenceSequenceExhausted(
            f"More than {ITEM_SEQUENCE_MAX} items on {tender_reference}."
        )
    return f"{tender_reference}-{value:03d}"


def _take_next(key, using):
    if not transaction.get_connection(using).in_atomic_block:
        raise TransactionManagementError(
            "References must be generated inside the transaction that creates "
            "the record (R20). Wrap the call in transaction.atomic()."
        )
    # Make sure the row exists. If two transactions race to create it, the
    # unique constraint makes one wait; get_or_create then catches the
    # IntegrityError and reads the committed row.
    ReferenceCounter.objects.using(using).get_or_create(key=key)
    # Lock the row until this transaction ends, then increment.
    counter = ReferenceCounter.objects.using(using).select_for_update().get(key=key)
    counter.last_value += 1
    counter.save(update_fields=["last_value"])
    return counter.last_value
