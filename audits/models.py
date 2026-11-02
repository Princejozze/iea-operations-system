"""
audits — app 7 in CLAUDE.md §1.

Holds: QCAudit, QCAuditLine, specification variance flagging.

May import from: core … items. Never from apps below it.
Every model holding business data inherits core.models.Auditable (R7).

Contract with tenders (read through reverse accessors; tenders never imports us):
    QCAudit.tender            ForeignKey(Tender, related_name="qc_audits")
    QCAudit.auditor           ForeignKey(User) — auditor.assert_can_audit(tender)
                              is called in clean() and save() on creation (R1)

Rules enforced here
  R1   the auditor must differ from the tender's project_manager, checked on the
       real foreign keys (``User.assert_can_audit``) when the audit is created.
       Re-assigning the tender later does not invalidate a completed audit.
  R2   gates ENTRY, in Tender.submit_for_audit. An audit can only be created while
       the tender is PENDING_AUDIT, so it can only exist on a fully priced sheet.
  R9   ``QCAudit.complete()`` with a NEGATIVE verdict calls
       Tender.record_audit_result(compliant=False), which returns the whole tender
       to COSTING and clears the rate lock (R4); this app records the reason and
       notifies the PM. A negative verdict is never a dead end.
  R9a  every write calls Tender.require_unfrozen().
  §14  the verdict is never automated. ``variance`` is a flag, not a judgement.
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone

from accounts.models import Role
from core.audit import audit_actor
from core.models import Auditable, AuditableQuerySet
from items.models import GuardedWrites, Item
from tenders.models import Tender, TenderStatus as S, TransitionError

from .variance import compare_specifications, describe


class Verdict(models.TextChoices):
    POSITIVE = "POSITIVE", "Positive"
    NEGATIVE = "NEGATIVE", "Negative"


class QCAuditQuerySet(AuditableQuerySet):
    def start(self, tender, auditor):
        """
        Open an audit of ``tender`` by ``auditor`` and prefill one line per item,
        in one transaction. Each line starts with the item's specification as
        ``specification_required``, what the selected quote offered as
        ``specification_offered`` (§5 step 13; empty if no quote is selected) and
        no verdict; the auditor reviews it and decides.
        """
        with transaction.atomic(using=self.db), audit_actor(auditor):
            audit = self.model(tender=tender, auditor=auditor)
            # Not full_clean(): it would wrap the typed R1/R9a/R2 errors and lose their
            # codes. save() raises them as they are.
            audit.save()
            for item in Item.objects.filter(tender=tender).order_by("item_reference"):
                QCAuditLine.objects.create(
                    audit=audit, item=item, specification_required=item.specification,
                    specification_offered=item.specification_offered,
                )
        return audit


class QCAudit(GuardedWrites, Auditable):
    """One auditor's pre-bid compliance review of a tender's item sheet (§3 "QC audit")."""

    GUARDED_FIELDS = ("verdict", "reason", "completed_at")

    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="qc_audits")
    auditor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    verdict = models.CharField(
        max_length=8, choices=Verdict.choices, blank=True, editable=False,
        help_text="System-set by complete(): NEGATIVE if any line is negative. Blank while open.",
    )
    reason = models.TextField(
        blank=True, editable=False, help_text="Why the audit was negative (R9). Set by complete()."
    )
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = QCAuditQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "QC audit"
        verbose_name_plural = "QC audits"
        constraints = [
            models.CheckConstraint(
                condition=Q(verdict__in=["", "POSITIVE", "NEGATIVE"]), name="audits_qcaudit_verdict_valid"
            ),
            # Completed exactly when it has a verdict.
            models.CheckConstraint(
                condition=(Q(verdict="") & Q(completed_at__isnull=True))
                | (~Q(verdict="") & Q(completed_at__isnull=False)),
                name="audits_qcaudit_completed_iff_verdict",
            ),
            # R9: a negative audit always records why.
            models.CheckConstraint(
                condition=~Q(verdict="NEGATIVE") | ~Q(reason=""), name="audits_qcaudit_negative_has_reason"
            ),
            # One audit at a time per tender.
            models.UniqueConstraint(
                fields=["tender"], condition=Q(verdict=""), name="audits_qcaudit_one_open_per_tender"
            ),
        ]

    def __str__(self):
        state = self.get_verdict_display() if self.verdict else "open"
        return f"QC audit of {self.tender.reference} ({state})"

    @property
    def is_complete(self):
        return bool(self.verdict)

    # -- invariants: clean() and save() (§4 Enforcement) ---------------------------

    def _creation_errors(self):
        """R1, R9a and the entry gate. Evaluated against the stored tender, never a cached copy."""
        tender = Tender._base_manager.get(pk=self.tender_id)
        tender.require_unfrozen("A QC audit")  # R9a
        if tender.status != S.PENDING_AUDIT:
            raise TransitionError(
                f"A QC audit can only be opened while the tender is PENDING_AUDIT, not {tender.status} "
                "(R2: the item sheet must be fully priced and submitted for audit first).",
                code="r2",
            )
        self.auditor.assert_can_audit(tender)  # R1, on the real foreign keys
        if type(self)._base_manager.filter(tender_id=self.tender_id, verdict="").exists():
            raise ValidationError("This tender already has an open QC audit.", code="open_audit")

    def clean(self):
        super().clean()
        if self._state.adding and self.tender_id and self.auditor_id:
            self._creation_errors()

    def save(self, *args, **kwargs):
        self._check_guard()
        with transaction.atomic():
            if self._state.adding:
                self._creation_errors()
            else:
                stored = type(self)._base_manager.get(pk=self.pk)
                if (stored.tender_id, stored.auditor_id) != (self.tender_id, self.auditor_id):
                    raise ValidationError("An audit's tender and auditor cannot be changed.")
                self.tender.require_unfrozen("Changing a QC audit")  # R9a
            super().save(*args, **kwargs)

    # -- completion: R9 -------------------------------------------------------------

    def complete(self, user, *, reason=""):
        """
        Close the audit. Only its auditor may. Every item needs a decided line.

        The audit's verdict is NEGATIVE if any line is, else POSITIVE. It is then
        handed to ``Tender.record_audit_result`` in the SAME transaction, so the
        audit and the tender's status can never disagree: if the tender refuses
        (wrong state, R1 again), the whole thing rolls back.

        NEGATIVE (R9): the tender returns to COSTING with its rate lock cleared,
        ``reason`` is recorded (defaults to the negative lines' comments, so it is
        never empty), and the PM is emailed once the transaction commits.
        """
        if user is None or not user.is_active or user.role != Role.AUDITOR or user.pk != self.auditor_id:
            raise PermissionDenied("Only this audit's own auditor can complete it.")
        with transaction.atomic(), audit_actor(user):
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if locked.is_complete:
                raise TransitionError("This audit is already complete.", code="r9")
            locked.tender.require_unfrozen("Completing a QC audit")  # R9a

            lines = list(locked.lines.select_related("item").order_by("item__item_reference"))
            item_ids = set(Item.objects.filter(tender_id=locked.tender_id).values_list("pk", flat=True))
            problems = []
            if {line.item_id for line in lines} != item_ids:
                problems.append("Every item on the sheet needs an audit line.")
            problems += [
                f"{line.item.item_reference}: no verdict yet." for line in lines if not line.verdict
            ]
            if problems:
                raise ValidationError(problems)

            negatives = [line for line in lines if line.verdict == Verdict.NEGATIVE]
            verdict = Verdict.NEGATIVE if negatives else Verdict.POSITIVE
            reason = reason.strip()
            if negatives and not reason:
                reason = "\n".join(f"{ln.item.item_reference}: {ln.comments.strip()}" for ln in negatives)

            with locked._system_write():
                locked.verdict = verdict
                locked.reason = reason if negatives else ""
                locked.completed_at = timezone.now()
                locked.save()

            # R9 / R4: the tender's own method moves the status and clears the lock.
            locked.tender.record_audit_result(user, compliant=not negatives)

            if negatives:
                _notify_pm_negative(locked, negatives)
        self.refresh_from_db()


def _notify_pm_negative(audit, negative_lines):
    """R9: tell the PM, with the reason, once the transition commits."""
    tender = Tender.objects.select_related("project_manager").get(pk=audit.tender_id)
    pm, reference, title = tender.project_manager, tender.reference, tender.title or "(untitled)"
    auditor = audit.auditor.get_full_name() or audit.auditor.email
    body = (
        f"{reference} — {title}\n\n"
        f"The QC audit by {auditor} was negative. The tender is back in COSTING, the "
        "exchange rates are unlocked, and you can correct the item sheet and submit it for audit again.\n\n"
        f"Reason:\n{audit.reason}\n\nLines that failed:\n"
        + "\n".join(f"- {ln.item.item_reference}: {ln.comments.strip()}" for ln in negative_lines)
        + "\n"
    )
    subject = f"Tender {reference}: QC audit negative — returned to COSTING"
    transaction.on_commit(lambda: send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [pm.email]))


class QCAuditLine(Auditable):
    """The audit of one item: what was required, what is offered, the flagged variance, the verdict."""

    audit = models.ForeignKey(QCAudit, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    specification_required = models.TextField(
        help_text="What the tender requires. Prefilled from the item's specification."
    )
    specification_offered = models.TextField(
        blank=True, help_text="What the supplier's product actually offers, as the auditor read it."
    )
    variance = models.JSONField(
        default=list, blank=True, editable=False,
        help_text="Numeric differences flagged automatically (audits.variance). A flag, not a judgement.",
    )
    verdict = models.CharField(
        max_length=8, choices=Verdict.choices, blank=True,
        help_text="The auditor's decision. Blank until decided; never set by the system (§14).",
    )
    comments = models.TextField(blank=True)

    class Meta:
        ordering = ["audit_id", "item__item_reference"]
        verbose_name = "QC audit line"
        constraints = [
            models.UniqueConstraint(fields=["audit", "item"], name="audits_qcauditline_one_per_item"),
            models.CheckConstraint(
                condition=Q(verdict__in=["", "POSITIVE", "NEGATIVE"]), name="audits_qcauditline_verdict_valid"
            ),
            # A negative line says why.
            models.CheckConstraint(
                condition=~Q(verdict="NEGATIVE") | ~Q(comments=""), name="audits_qcauditline_negative_has_comments"
            ),
        ]

    def __str__(self):
        return f"{self.item.item_reference}: {self.verdict or 'undecided'}"

    @property
    def has_variance(self):
        return bool(self.variance)

    @property
    def variance_summary(self):
        return [describe(flag) for flag in self.variance]

    def _invariant_errors(self):
        errors = {}
        if self.item_id and self.audit_id and self.item.tender_id != self.audit.tender_id:
            errors["item"] = "The item belongs to a different tender than the audit."
        if self.verdict == Verdict.NEGATIVE and not self.comments.strip():
            errors["comments"] = "Say why: a negative line needs a comment (it becomes the R9 reason)."
        return errors

    def clean(self):
        super().clean()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            audit = QCAudit._base_manager.select_related("tender").get(pk=self.audit_id)
            if audit.is_complete:
                raise ValidationError("This audit is complete; its lines can no longer change.")
            audit.tender.require_unfrozen("Editing a QC audit line")  # R9a
            errors = self._invariant_errors()
            if errors:
                raise ValidationError(errors)
            if not self.specification_required and self.item_id:
                self.specification_required = self.item.specification
            # Always recomputed: variance is derived, never entered (like R10 for money).
            self.variance = compare_specifications(self.specification_required, self.specification_offered)
            super().save(*args, **kwargs)


# A completed audit and its lines are a record: nothing deletes them, whether by
# instance, queryset or cascade (pre_delete fires in all three). R7 still audits
# the deletion of an open one.


@receiver(pre_delete, sender=QCAudit, dispatch_uid="audits.protect_completed_audit")
def _protect_completed_audit(sender, instance, **kwargs):
    if sender._base_manager.filter(pk=instance.pk).exclude(verdict="").exists():
        raise ValidationError("A completed QC audit is a record and cannot be deleted.")


@receiver(pre_delete, sender=QCAuditLine, dispatch_uid="audits.protect_completed_lines")
def _protect_completed_lines(sender, instance, **kwargs):
    if QCAudit._base_manager.filter(pk=instance.audit_id).exclude(verdict="").exists():
        raise ValidationError("A completed QC audit's lines cannot be deleted.")
