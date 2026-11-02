"""
Letterhead rendering from a plain context dict.

``render(kind, context)`` takes a string kind and a dict of primitives and
returns text and HTML. It touches no model, so it is usable from a management
command, a view or a test with nothing in the database.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings
from django.template.loader import render_to_string

from .maf import validate_maf_context
from .pdf import html_to_pdf
from .resolution import validate_resolution_context
from .rfq import SALES_CC, validate_rfq_context

# Shown when settings.DOCUMENT_LETTERHEAD does not override it. Address and phone
# are deliberately absent: they are not in the spec and must not be invented.
# Set them in settings.DOCUMENT_LETTERHEAD = {"address": "...", "phone": "..."}.
LETTERHEAD_DEFAULTS = {
    "company_name": "Imperial East Africa Limited",
    "email": SALES_CC,
    "address": "",
    "phone": "",
}


def letterhead():
    """The company block printed at the top of every document."""
    return {**LETTERHEAD_DEFAULTS, **getattr(settings, "DOCUMENT_LETTERHEAD", {})}


@dataclass(frozen=True)
class RenderedDocument:
    subject: str
    text: str  # plain-text body; empty for kinds that are PDF-only
    html: str
    context: dict  # the validated, JSON-clean context actually rendered


@dataclass(frozen=True)
class Kind:
    """How one document kind renders. ``text_template`` is None for PDF-only kinds."""

    validate: Callable[[dict], dict]
    subject: str  # format string over the validated context
    html_template: str
    text_template: str | None = None


# kind (the DocumentKind value, as a plain string) -> how to render it
KINDS = {
    "RFQ": Kind(
        validate_rfq_context,
        "Request for Quotation - {tender_reference}",
        "documents/rfq.html",
        "documents/rfq.txt",
    ),
    "MAF": Kind(
        validate_maf_context,
        "Manufacturer's Authorisation Form - {tender_reference}",
        "documents/maf.html",
    ),
    "BOARD_RESOLUTION": Kind(
        validate_resolution_context,
        "Board Resolution - {tender_reference}",
        "documents/resolution.html",
    ),
}


def render(kind, context):
    """Render ``context`` as document ``kind``. Raises ValidationError on a bad
    context and KeyError on a kind with no template."""
    try:
        spec = KINDS[str(kind)]
    except KeyError:
        raise KeyError(f"No letterhead template for document kind {kind!r}.") from None
    context = spec.validate(context)
    # One line, always: a subject never carries a newline from user data.
    subject = " ".join(spec.subject.format(**context).split())
    page = {"letterhead": letterhead(), "doc": context, "title": subject}
    return RenderedDocument(
        subject=subject,
        text=render_to_string(spec.text_template, page) if spec.text_template else "",
        html=render_to_string(spec.html_template, page),
        context=context,
    )


def render_pdf(kind, context):
    """The kind rendered straight to PDF bytes."""
    return html_to_pdf(render(kind, context).html)
