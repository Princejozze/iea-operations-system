"""
documents — app 3 in CLAUDE.md §1.

Holds: Letterhead template rendering and the Document registry.

Renders from a plain context dict and NEVER imports a domain model or enum
(tenders, items, suppliers, ...). That is what keeps this app independent of
everything below it: callers flatten their records into primitives and hand
them in. ``documents.tests.test_independence`` enforces it.

May import from: core, accounts. Never from apps below it.
Every model holding business data inherits core.models.Auditable (R7).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from core.models import Auditable

from .rfq import SALES_CC, normalise_recipients, with_required_cc


class DocumentKind(models.TextChoices):
    """
    Local to this app, deliberately not shared with any domain app.

    Only kinds that have a template exist here. The rest of CLAUDE.md §10's list
    (bid security request, bank letters, ...) are added one at a time as their
    templates are built — do not add a kind without its template.
    """

    RFQ = "RFQ", "Request for Quotation"
    MAF = "MAF", "Manufacturer's Authorisation Form"
    BOARD_RESOLUTION = "BOARD_RESOLUTION", "Board Resolution"


class Document(Auditable):
    """
    One generated document — the registry entry (§10: "Each creates a Document
    registry entry automatically").

    It stores the plain context the document was rendered from, plus the output
    exactly as produced, so what was sent months ago can be reproduced even if
    a template later changes. Create through ``documents.services``, not by hand.
    """

    # Rendered output is bulky and fully derivable from ``context``; the audit
    # trail records the context, recipients and subject instead.
    audit_exclude = ("updated_at", "body_text", "body_html")

    kind = models.CharField(max_length=16, choices=DocumentKind.choices)
    title = models.CharField(max_length=255)
    related_reference = models.CharField(
        max_length=64,
        blank=True,
        help_text="Plain text reference of what this is about, e.g. a tender reference. "
        "Not a foreign key: this app cannot import the domain models.",
    )
    context = models.JSONField(
        default=dict, help_text="The plain dict the document was rendered from."
    )
    to = models.JSONField(default=list, blank=True, help_text="Recipient addresses.")
    cc = models.JSONField(
        default=list,
        blank=True,
        help_text="Always contains the sales mailbox on an RFQ (R8); the system adds it.",
    )
    subject = models.CharField(max_length=255, blank=True)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)
    pdf = models.FileField(
        upload_to="documents/%Y/%m/",
        max_length=255,
        blank=True,
        help_text="The PDF exactly as generated (via STORAGES, so S3 in production).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Empty means the system generated it (e.g. a cron job).",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["kind", "related_reference"])]
        constraints = [
            # R8, last line of defence: even raw SQL cannot store an RFQ whose
            # cc list lacks the sales mailbox. (Stored lower-case, see rfq.py.)
            models.CheckConstraint(
                condition=~Q(kind="RFQ") | Q(cc__contains=[SALES_CC]),
                name="documents_document_rfq_cc_sales",
            ),
        ]

    def __str__(self):
        return self.title

    # -- R8, enforced per CLAUDE.md §4: clean(), repeated in save(), DB constraint --

    def _normalise_recipients(self):
        self.to = normalise_recipients(self.to)
        self.cc = normalise_recipients(self.cc)
        if self.kind == DocumentKind.RFQ:
            self.cc = with_required_cc(self.cc)

    def clean(self):
        super().clean()
        try:
            self._normalise_recipients()
        except ValidationError as exc:
            raise ValidationError({"to": exc.messages}) from exc

    def save(self, *args, **kwargs):
        # The system adds the sales mailbox; nothing a caller passes can drop it.
        self._normalise_recipients()
        super().save(*args, **kwargs)
