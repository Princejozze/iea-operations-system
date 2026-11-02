"""
Manufacturer's Authorisation Form (MAF) context contract.

A MAF is the manufacturer's statement that IEA may bid for, and supply, their
goods against a named tender. IEA prepares it pre-filled; the manufacturer signs.
"""

from .contracts import validate_context

REQUIRED_KEYS = ("manufacturer_name", "tender_reference", "procuring_entity")
REQUIRED_GOODS_KEYS = ("description",)


def validate_maf_context(context):
    """
    Required: manufacturer_name, tender_reference, procuring_entity, and a
    non-empty ``goods`` list whose rows each have a ``description``.
    Optional: manufacturer_address, tender_title, bidder_name (defaults to the
    letterhead company name), date, signatory_name, signatory_title (left as
    blank lines to fill in by hand when absent), and per-row ``model``.
    """
    return validate_context(
        context, label="MAF", required=REQUIRED_KEYS, rows=("goods", REQUIRED_GOODS_KEYS, "Goods row")
    )
