"""
Board resolution (authority to tender) context contract.
"""

from .contracts import validate_context

REQUIRED_KEYS = ("tender_reference", "procuring_entity", "signatory_name", "signatory_title")
REQUIRED_DIRECTOR_KEYS = ("name",)


def validate_resolution_context(context):
    """
    Required: tender_reference, procuring_entity, signatory_name and
    signatory_title (who the Board authorises), and a non-empty ``directors``
    list (each with a ``name``) who sign the resolution.
    Optional: tender_title, company_registration_number, date (when the Board
    resolved; a blank line when absent), and per-director ``title``.
    """
    return validate_context(
        context,
        label="board resolution",
        required=REQUIRED_KEYS,
        rows=("directors", REQUIRED_DIRECTOR_KEYS, "Director"),
    )
