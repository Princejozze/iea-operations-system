"""
Letterhead rendering from a plain context dict.

``render(kind, context)`` takes a string kind and a dict of primitives and
returns text and HTML. It touches no model, so it is usable from a management
command, a view or a test with nothing in the database.
"""

from dataclasses import dataclass

from django.conf import settings
from django.template.loader import render_to_string

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
    text: str
    html: str
    context: dict  # the validated, JSON-clean context actually rendered


def _rfq(context):
    context = validate_rfq_context(context)
    # One line, always: a subject never carries a newline from user data.
    subject = " ".join(f"Request for Quotation - {context['tender_reference']}".split())
    page = {"letterhead": letterhead(), "doc": context, "title": subject}
    return RenderedDocument(
        subject=subject,
        text=render_to_string("documents/rfq.txt", page),
        html=render_to_string("documents/rfq.html", page),
        context=context,
    )


# kind (the DocumentKind value, as a plain string) -> renderer
RENDERERS = {"RFQ": _rfq}


def render(kind, context):
    """Render ``context`` as document ``kind``. Raises ValidationError on a bad
    context and KeyError on a kind with no template."""
    try:
        renderer = RENDERERS[str(kind)]
    except KeyError:
        raise KeyError(f"No letterhead template for document kind {kind!r}.") from None
    return renderer(context)
