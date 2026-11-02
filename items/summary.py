"""
What the item sheet adds up to (§4: "A tender's bid value is the sum of its items'
total_selling. VAT (18%) is applied at bid-summary level, not per item.")

Read by the item sheet header and, from the apps below, by the MD's submission
approval screen — one definition, so the figure the PM sees and the figure the MD
signs can never differ.

Each item carries the VAT rate it was costed with (``Item.vat_rate``, kept "for the
record"), so VAT is summed per item at its own rate rather than taken from today's
defaults: editing a default never silently re-prices a bid already costed (§4).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Q

from tenders.models import (
    R2_REQUIRED_ATTACHMENT_KINDS,
    R2_REQUIRED_TEXT_FIELDS,
    R2_REQUIRED_VALUE_FIELDS,
)

ZERO = Decimal(0)
WHOLE = Decimal(1)

# R2's fields, in words a PM uses (the field names are for developers).
MISSING_LABELS = {
    "unit_cost": "price", "supplier": "supplier", "currency": "currency", "incoterm": "Incoterm",
    "unit_of_measure": "unit", "category": "category", "make_brand": "make",
    "model_part_number": "model", "country_of_origin": "origin",
}
ATTACHMENT_LABELS = {"BROCHURE": "brochure", "QUOTATION": "quotation", "DATASHEET": "datasheet"}


def whole_tzs(value):
    """Round to whole shillings for display; sub-shilling precision is kept in the database."""
    return (value or ZERO).quantize(WHOLE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class BidSummary:
    items_total: int
    items_priced: int
    subtotal: Decimal  # sum of total_selling_price_tzs, ex VAT
    vat: Decimal
    total: Decimal  # incl. VAT

    @property
    def items_unpriced(self):
        return self.items_total - self.items_priced

    @property
    def percent_priced(self):
        return int(100 * self.items_priced / self.items_total) if self.items_total else 0

    @property
    def vat_percent(self):
        return (self.vat / self.subtotal * 100).quantize(Decimal("0.01")) if self.subtotal else None


def bid_summary(tender):
    """The tender's items rolled up. Goes through ``tender.items`` (§1: no import of Item)."""
    items = tender.items.all()
    counts = items.aggregate(total=Count("pk"), priced=Count("pk", filter=Q(unit_cost__isnull=False)))
    subtotal = vat = ZERO
    for total, rate in items.filter(total_selling_price_tzs__isnull=False).values_list(
        "total_selling_price_tzs", "vat_rate"
    ):
        subtotal += total
        vat += total * (rate or ZERO)
    return BidSummary(
        items_total=counts["total"], items_priced=counts["priced"],
        subtotal=whole_tzs(subtotal), vat=whole_tzs(vat), total=whole_tzs(subtotal) + whole_tzs(vat),
    )


def missing_for_audit(item, attachment_kinds=None):
    """What R2 still needs on this item, as short labels. Empty means ready for audit.

    ``attachment_kinds`` may be passed in (a set) to avoid a query per item.
    """
    missing = []
    for name in R2_REQUIRED_VALUE_FIELDS:  # null = missing; supplier is a foreign key
        column = name if name == "unit_cost" else f"{name}_id"
        if getattr(item, column) is None:
            missing.append(MISSING_LABELS[name])
    for name in R2_REQUIRED_TEXT_FIELDS:
        if not getattr(item, name):
            missing.append(MISSING_LABELS[name])
    if attachment_kinds is None:
        attachment_kinds = {a.kind for a in item.attachments.all()}
    missing += [ATTACHMENT_LABELS[k] for k in R2_REQUIRED_ATTACHMENT_KINDS if k not in attachment_kinds]
    return missing


def blocker_counts(tender):
    """For the tender page: how many items lack each thing R2 needs, in words — a short
    list of "12 need a price", not a line per developer field name.

    Returns [(label, count), ...], most numerous first. Empty when ready for audit.
    """
    items = list(tender.items.prefetch_related("attachments"))
    counts = {}
    for item in items:
        for label in missing_for_audit(item, {a.kind for a in item.attachments.all()}):
            counts[label] = counts.get(label, 0) + 1
    # Most numerous first; ties in the order a PM works through them (price before documents).
    order = [*MISSING_LABELS.values(), *ATTACHMENT_LABELS.values()]
    return sorted(counts.items(), key=lambda kv: (-kv[1], order.index(kv[0])))
