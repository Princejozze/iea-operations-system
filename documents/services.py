"""
Create registry entries and outgoing mail for generated documents.

Other apps call ``create_document`` with a flattened dict; they never write a
``Document`` row themselves, so the render, the registry entry and R8 cannot
drift apart.
"""

from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils.text import slugify

from .models import Document, DocumentKind
from .pdf import html_to_pdf
from .rendering import render
from .rfq import normalise_recipients, with_required_cc


def pdf_filename(kind, reference):
    return f"{slugify(str(kind))}-{slugify(reference)[:40] or 'document'}.pdf"


def create_document(kind, context, *, to=(), cc=(), user=None, related_reference=""):
    """
    Render and register a document in one transaction; return the ``Document``.

    Every kind is also rendered to PDF and stored on ``Document.pdf``.
    For an RFQ the sales mailbox is added to ``cc`` here, in ``Document.save()``
    and by a database constraint (R8) — passing ``cc=[]`` does not remove it.
    ``user`` becomes ``created_by`` and the audit actor; None means the system.
    """
    rendered = render(kind, context)
    reference = related_reference or str(rendered.context.get("tender_reference", ""))
    document = Document(
        kind=kind,
        title=rendered.subject,
        related_reference=reference[:64],
        context=rendered.context,
        to=normalise_recipients(to),
        cc=normalise_recipients(cc),
        subject=rendered.subject,
        body_text=rendered.text,
        body_html=rendered.html,
        created_by=user,
    )
    with transaction.atomic():
        # Validate before the PDF is written to storage, so a rejected document
        # leaves no orphan file behind.
        document.full_clean(exclude=["created_by", "pdf"])
        document.pdf.save(
            pdf_filename(kind, reference), ContentFile(html_to_pdf(rendered.html)), save=False
        )
        document.save(actor=user)
    return document


def build_email(document, *, from_email=None):
    """
    The unsent email for a registered document. Recipients are re-derived from
    the stored lists through the same R8 rule, so a row edited behind the app's
    back still goes out with the sales cc. Sending stays with the caller.
    """
    cc = normalise_recipients(document.cc)
    if document.kind == DocumentKind.RFQ:
        cc = with_required_cc(cc)
    message = EmailMultiAlternatives(
        subject=document.subject,
        body=document.body_text,
        from_email=from_email,
        to=normalise_recipients(document.to),
        cc=cc,
    )
    message.attach_alternative(document.body_html, "text/html")
    if document.pdf:
        with document.pdf.open("rb") as handle:
            message.attach(
                pdf_filename(document.kind, document.related_reference),
                handle.read(),
                "application/pdf",
            )
    return message
