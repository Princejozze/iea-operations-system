"""
RFQ rules that do not need a database: the R8 cc, recipient handling and the
context contract. Pure Python so ``models`` and ``services`` can both use it.

R8 (CLAUDE.md §9): RFQs must cc sales@imperialeastafrica.tz. The SOP voids an
offer not copied there. The system adds it; the user cannot remove it.
"""

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .contracts import validate_context

# R8. Stored and compared lower-case.
SALES_CC = "sales@imperialeastafrica.tz"


def normalise_recipients(addresses):
    """
    Clean an address list: strip, validate, drop blanks, de-duplicate ignoring
    case (first spelling wins). Accepts a list or a single string.

    Raises ValidationError on an invalid address. validate_email rejects
    newlines and commas, so this is also the header-injection guard.
    """
    if addresses is None:
        return []
    if isinstance(addresses, str):
        addresses = [addresses]
    if not isinstance(addresses, (list, tuple)):
        raise ValidationError("Recipients must be a list of email addresses.")
    seen, out = set(), []
    for raw in addresses:
        if not isinstance(raw, str):
            raise ValidationError("Recipients must be email address strings.")
        address = raw.strip()
        if not address:
            continue
        validate_email(address)
        if address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    return out


def with_required_cc(cc):
    """``cc`` with the sales mailbox guaranteed present (R8), first in the list."""
    rest = [a for a in normalise_recipients(cc) if a.lower() != SALES_CC]
    return [SALES_CC, *rest]


# -- context contract -------------------------------------------------------

REQUIRED_KEYS = ("supplier_name", "tender_reference", "sender_name")
REQUIRED_ITEM_KEYS = ("reference", "specification", "quantity")


def validate_rfq_context(context):
    """
    Check an RFQ context dict; return it as a deep, JSON-clean copy.

    Required: supplier_name, tender_reference, sender_name, and a non-empty
    ``items`` list whose rows each have reference, specification and quantity.
    Optional: supplier_contact, sender_title, date, reply_by, message,
    and per-item unit_of_measure and make_brand. Dates are pre-formatted strings.
    """
    return validate_context(
        context, label="RFQ", required=REQUIRED_KEYS, rows=("items", REQUIRED_ITEM_KEYS, "Item")
    )
