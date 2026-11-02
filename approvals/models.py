"""
approvals — app 8 in CLAUDE.md §1.

Holds: Approval (REGISTRATION | SUBMISSION), EligibilityReview, FundRequest.

May import from: core … audits. Never from apps below it.
Every model holding business data inherits core.models.Auditable (R7).

Contract with tenders (read by Tender's transition methods through reverse
accessors, so tenders never imports this app — §1):
    Approval.tender           ForeignKey(Tender, related_name="approvals")
                              fields: kind, decision, decided_by, decided_at, comments
    EligibilityReview.tender  ForeignKey(Tender, related_name="eligibility_reviews")
                              fields: raised_by, md_decision ("" while open), decided_by, decided_at
    FundRequest.tender        ForeignKey(Tender, related_name="fund_requests")
                              fields: purpose, status

How the MD's decisions reach the tender. Each decision is recorded and acted
on in ONE transaction, so a decision and the tender's status can never disagree:

    Approval.objects.sign(...)            records the signed Approval, then calls
                                          Tender.approve_registration / approve_submission
    review.decide(...)                    records the MD's decision, then calls
                                          Tender.resolve_eligibility
    EligibilityReview.objects.raise_for() records the review, then calls
                                          Tender.raise_eligibility

If the tender refuses the transition (R15, R3, R16 ...) the whole transaction
rolls back, including the record. Status is still written only by Tender (R19).
"""

from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Role, User
from core.audit import audit_actor, get_current_actor
from core.models import Auditable, AuditableQuerySet
from tenders.models import (
    ELIGIBILITY_ENTRY_STATUSES,
    Currency,
    GuardedFieldError,
    Tender,
    TenderStatus as S,
    TransitionError,
)

# ---------------------------------------------------------------------------
# Vocabularies (§7, §8)
# ---------------------------------------------------------------------------


class ApprovalKind(models.TextChoices):
    # There is deliberately no ELIGIBILITY kind: those decisions live on
    # EligibilityReview, and fund approvals on FundRequest (§7).
    REGISTRATION = "REGISTRATION", "Registration"
    SUBMISSION = "SUBMISSION", "Submission"


class ApprovalDecision(models.TextChoices):
    APPROVED = "APPROVED", "Approved"
    DECLINED = "DECLINED", "Declined"


class EligibilityDecision(models.TextChoices):
    PROCEED = "PROCEED", "Proceed"
    JOINT_VENTURE = "JOINT_VENTURE", "Joint venture"
    REMEDIATE = "REMEDIATE", "Remediate"
    WITHDRAW = "WITHDRAW", "Withdraw"


class FundRequestPurpose(models.TextChoices):
    NEST_APPLICATION_FEE = "NEST_APPLICATION_FEE", "NeST application fee"
    TENDER_DOCUMENT_FEE = "TENDER_DOCUMENT_FEE", "Tender document fee"
    BID_SECURITY = "BID_SECURITY", "Bid security"
    OTHER = "OTHER", "Other"


class FundRequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    DECLINED = "DECLINED", "Declined"
    PAID = "PAID", "Paid"


# The tender state each kind of Approval may be signed in.
APPROVAL_TENDER_STATUS = {
    ApprovalKind.REGISTRATION: S.PENDING_REGISTRATION_APPROVAL,
    ApprovalKind.SUBMISSION: S.PENDING_EXECUTIVE_APPROVAL,
}
# A fund request may be raised once the MD has registered the tender and until
# it is submitted or ends. (Not stated in the spec — see the build notes.)
FUND_REQUEST_TENDER_STATUSES = (
    S.DRAFTING,
    S.COSTING,
    S.PENDING_ELIGIBILITY_DECISION,
    S.PENDING_AUDIT,
    S.PENDING_EXECUTIVE_APPROVAL,
    S.READY_TO_SUBMIT,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fresh_tender_status(tender):
    """The status as stored, never a cached copy on an instance."""
    return Tender._base_manager.filter(pk=tender.pk).values_list("status", flat=True).first()


def _is_active(user, role):
    return user is not None and user.is_active and user.role == role


def _send_on_commit(subject, body, recipients):
    recipients = sorted({r for r in recipients if r})
    if recipients:
        transaction.on_commit(
            lambda: send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients)
        )


def _emails(role):
    return list(User.objects.filter(role=role, is_active=True).values_list("email", flat=True))


class GuardedWrites:
    """
    Fields that only a method of the model itself may write (§4 Enforcement:
    status is never set by assignment, admin or ``queryset.update()``).

    ``save()`` calls ``_check_guard()``: outside ``with self._system_write():``
    a new row must carry the defaults for ``GUARDED_FIELDS``, and an existing
    row must not change them.
    """

    GUARDED_FIELDS = ()
    _guard_open = False

    @contextmanager
    def _system_write(self):
        self._guard_open = True
        try:
            yield
        finally:
            self._guard_open = False

    def _check_guard(self):
        if self._guard_open:
            return
        fields = [self._meta.get_field(name) for name in self.GUARDED_FIELDS]
        if self._state.adding:
            stored = {f.attname: f.get_default() for f in fields}
        else:
            stored = type(self)._base_manager.filter(pk=self.pk).values(*[f.attname for f in fields]).first()
            if stored is None:
                return
        errors = {
            f.name: f"{f.name} is set only by the model's own methods."
            for f in fields
            if stored[f.attname] != getattr(self, f.attname)
        }
        if errors:
            raise GuardedFieldError(errors)


# ---------------------------------------------------------------------------
# Approval — the MD's REGISTRATION and SUBMISSION decisions (R15, R3, R9)
# ---------------------------------------------------------------------------


class ApprovalQuerySet(AuditableQuerySet):
    def delete(self):
        raise PermissionDenied("An Approval is a signed decision and cannot be deleted.")

    delete.queryset_only = True


class ApprovalManager(models.Manager.from_queryset(ApprovalQuerySet)):
    def sign(self, tender, kind, decision, user, *, typed_name, password, comments=""):
        """
        The only way an Approval is created. Verifies the signature at this
        moment (§4): the approver's typed full name and their password,
        re-entered. Only the typed name is stored — never the password.

        Then acts on the tender in the same transaction: REGISTRATION calls
        ``approve_registration`` (R15); SUBMISSION calls ``approve_submission``
        (R3, and R9 when declined). A refused transition rolls the Approval back.
        """
        if not _is_active(user, Role.EXECUTIVE):
            raise PermissionDenied("Only the Managing Director can sign an approval.")
        if not user.check_password(password or ""):
            raise PermissionDenied("The signature does not verify: re-enter your password.")
        if " ".join((typed_name or "").split()).casefold() != " ".join(user.get_full_name().split()).casefold():
            raise PermissionDenied("The signature does not verify: type your full name exactly.")
        with transaction.atomic(), audit_actor(user):
            approval = self.model(
                tender=tender, kind=kind, decision=decision, decided_by=user,
                comments=(comments or "").strip(), signature=" ".join(typed_name.split()),
            )
            with approval._system_write():
                approval.save()
            if kind == ApprovalKind.REGISTRATION:
                tender.approve_registration(user)
            elif kind == ApprovalKind.SUBMISSION:
                tender.approve_submission(user)
        return approval


class Approval(GuardedWrites, Auditable):
    """One signed MD decision. Immutable once made: a changed mind is a new
    Approval, never an edit (a declined submission is signed afresh next round)."""

    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="approvals")
    kind = models.CharField(max_length=16, choices=ApprovalKind.choices)
    decision = models.CharField(max_length=16, choices=ApprovalDecision.choices)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(default=timezone.now, editable=False)
    comments = models.TextField(blank=True, help_text="Required when declining: this is the reason the PM is told (R9).")
    signature = models.CharField(
        max_length=255, help_text="The approver's typed full name. The password is verified, never stored (§4)."
    )

    objects = ApprovalManager()

    class Meta:
        ordering = ["-decided_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=ApprovalKind.values), name="approvals_approval_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(decision__in=ApprovalDecision.values), name="approvals_approval_decision_valid"
            ),
            models.CheckConstraint(
                condition=Q(decision=ApprovalDecision.APPROVED) | ~Q(comments=""),
                name="approvals_approval_decline_has_reason",
            ),
            models.CheckConstraint(condition=~Q(signature=""), name="approvals_approval_signed"),
        ]

    def __str__(self):
        return f"{self.tender.reference}: {self.get_kind_display()} {self.get_decision_display()}"

    def _invariant_errors(self):
        errors = {}
        if not _is_active(self.decided_by if self.decided_by_id else None, Role.EXECUTIVE):
            errors["decided_by"] = "Only an active Executive can decide an approval."
        if self.kind not in ApprovalKind.values:
            errors["kind"] = "Unknown approval kind."
        if self.decision not in ApprovalDecision.values:
            errors["decision"] = "Unknown decision."
        if self.decision == ApprovalDecision.DECLINED and not self.comments.strip():
            errors["comments"] = "A decline must record its reason (R9)."
        if not self.signature.strip():
            errors["signature"] = "An approval must be signed."
        if self._state.adding and self.kind in APPROVAL_TENDER_STATUS:
            expected = APPROVAL_TENDER_STATUS[self.kind]
            actual = _fresh_tender_status(self.tender)
            if actual != expected:
                errors["tender"] = f"A {self.kind} approval can only be signed while the tender is {expected}, not {actual}."
        return errors

    def clean(self):
        super().clean()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A signed approval cannot be changed.")
        if not self._guard_open:
            raise GuardedFieldError({"signature": "An Approval is created only by Approval.objects.sign()."})
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("An Approval is a signed decision and cannot be deleted.")


# ---------------------------------------------------------------------------
# EligibilityReview — R16
# ---------------------------------------------------------------------------


class EligibilityReviewManager(models.Manager.from_queryset(AuditableQuerySet)):
    def raise_for(self, tender, user, *, requirement, pm_notes=""):
        """
        R16: the PM escalates an unmet special requirement. Records the review
        and moves the tender to PENDING_ELIGIBILITY_DECISION in one transaction
        (``Tender.raise_eligibility``). The PM can neither silently proceed nor
        silently abandon.
        """
        if not _is_active(user, Role.PROJECT_MANAGER):
            raise PermissionDenied("Only a Project Manager can raise an eligibility review.")
        with transaction.atomic(), audit_actor(user):
            review = self.model(tender=tender, requirement=(requirement or "").strip(),
                                raised_by=user, pm_notes=(pm_notes or "").strip())
            review.save()
            tender.raise_eligibility(user)
        return review


class EligibilityReview(GuardedWrites, Auditable):
    """An unmet special requirement and the MD's ruling on it. ``md_decision``
    is empty while the review is open."""

    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="eligibility_reviews")
    requirement = models.TextField(help_text="The special / minimum requirement IEA does not meet.")
    raised_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    pm_notes = models.TextField(blank=True)
    md_decision = models.CharField(max_length=16, choices=EligibilityDecision.choices, blank=True)
    jv_partner = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="The firm to partner with. Set only when the decision is JOINT_VENTURE.",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_notes = models.TextField(blank=True)

    objects = EligibilityReviewManager()
    GUARDED_FIELDS = ("md_decision", "jv_partner", "decided_by", "decided_at", "decision_notes")

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(md_decision__in=["", *EligibilityDecision.values]),
                name="approvals_eligibility_decision_valid",
            ),
            # Open ⇒ nothing decided. Decided ⇒ who and when. JV partner ⇔ JOINT_VENTURE.
            models.CheckConstraint(
                condition=(
                    Q(md_decision="", decided_by__isnull=True, decided_at__isnull=True,
                      jv_partner__isnull=True, decision_notes="")
                    | (
                        ~Q(md_decision="") & Q(decided_by__isnull=False, decided_at__isnull=False)
                        & (
                            Q(md_decision=EligibilityDecision.JOINT_VENTURE, jv_partner__isnull=False)
                            | (~Q(md_decision=EligibilityDecision.JOINT_VENTURE) & Q(jv_partner__isnull=True))
                        )
                    )
                ),
                name="approvals_eligibility_decision_consistent",
            ),
            # A tender has at most one undecided review at a time.
            models.UniqueConstraint(
                fields=["tender"], condition=Q(md_decision=""), name="approvals_eligibility_one_open_per_tender"
            ),
        ]

    def __str__(self):
        state = self.get_md_decision_display() if self.md_decision else "open"
        return f"{self.tender.reference}: eligibility review ({state})"

    @property
    def is_open(self):
        return self.md_decision == ""

    def _invariant_errors(self):
        errors = {}
        if not self.requirement.strip():
            errors["requirement"] = "State the requirement that is not met."
        if self._state.adding:
            pm = self.raised_by if self.raised_by_id else None
            if not _is_active(pm, Role.PROJECT_MANAGER):
                errors["raised_by"] = "Only a Project Manager can raise an eligibility review."
            elif pm.pk != self.tender.project_manager_id:
                errors["raised_by"] = "Only this tender's PM can raise its eligibility review."
            actual = _fresh_tender_status(self.tender)
            if actual not in ELIGIBILITY_ENTRY_STATUSES:
                errors["tender"] = f"An eligibility review can be raised only from DRAFTING or COSTING, not {actual}."
            elif self.md_decision == "" and type(self)._base_manager.filter(tender=self.tender, md_decision="").exists():
                errors["tender"] = "This tender already has an open eligibility review."
        return errors

    def clean(self):
        super().clean()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._check_guard()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)
        super().save(*args, **kwargs)

    def decide(self, user, decision, *, notes="", jv_partner=None):
        """
        R16: only the MD resolves a review. Records the decision and returns
        the tender to where it came from — or withdraws it — in one transaction
        (``Tender.resolve_eligibility``). JOINT_VENTURE needs a partner; no
        other decision may name one.
        """
        if not _is_active(user, Role.EXECUTIVE):
            raise PermissionDenied("Only the Managing Director can resolve an eligibility review (R16).")
        if decision not in EligibilityDecision.values:
            raise ValidationError({"md_decision": f"Unknown decision {decision!r}."})
        if decision == EligibilityDecision.JOINT_VENTURE and jv_partner is None:
            raise ValidationError({"jv_partner": "A joint venture decision must name the partner."})
        if decision != EligibilityDecision.JOINT_VENTURE and jv_partner is not None:
            raise ValidationError({"jv_partner": "Only a joint venture decision names a partner."})
        with transaction.atomic(), audit_actor(user):
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if not locked.is_open:
                raise TransitionError("This eligibility review has already been decided (R16).", code="r16")
            if _fresh_tender_status(locked.tender) != S.PENDING_ELIGIBILITY_DECISION:
                raise TransitionError("The tender is not awaiting an eligibility decision (R16).", code="r16")
            locked.md_decision = decision
            locked.jv_partner = jv_partner
            locked.decided_by = user
            locked.decided_at = timezone.now()
            locked.decision_notes = (notes or "").strip()
            with locked._system_write():
                locked.save()
            locked.tender.resolve_eligibility(user)
        self.refresh_from_db()


# ---------------------------------------------------------------------------
# FundRequest — PM requests, MD approves, Finance pays (§3 "Application fund", R17)
# ---------------------------------------------------------------------------


class FundRequest(GuardedWrites, Auditable):
    """
    Money needed for a tender. PM raises it, EXEC moves it to APPROVED (or
    DECLINED), FIN moves it to PAID. R17 reads only the PAID, NEST_APPLICATION_FEE
    ones — see ``Tender.has_nest_funds`` — which is why Finance never writes to Tender.
    """

    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="fund_requests")
    purpose = models.CharField(max_length=24, choices=FundRequestPurpose.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(default=timezone.now, editable=False)
    status = models.CharField(
        max_length=16, choices=FundRequestStatus.choices, default=FundRequestStatus.PENDING, editable=False
    )
    # decided_*, not approved_*: these are set on a DECLINED request too, and "approved_by" on a
    # declined row reads as a lie in the audit trail (§7). Same names as Approval and EligibilityReview.
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        editable=False, help_text="The MD who decided: set on APPROVED and also on DECLINED.",
    )
    decided_at = models.DateTimeField(null=True, blank=True, editable=False)
    decision_reason = models.TextField(
        blank=True, editable=False,
        help_text="The MD's reason. Required when declining: the PM is told it.",
    )
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        editable=False,
    )
    paid_at = models.DateTimeField(null=True, blank=True, editable=False)
    payment_reference = models.CharField(max_length=100, blank=True, editable=False)
    notes = models.TextField(blank=True)

    objects = AuditableQuerySet.as_manager()
    GUARDED_FIELDS = (
        "status", "decided_by", "decided_at", "decision_reason", "paid_by", "paid_at", "payment_reference",
    )

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(purpose__in=FundRequestPurpose.values), name="approvals_fundrequest_purpose_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=FundRequestStatus.values), name="approvals_fundrequest_status_valid"
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="approvals_fundrequest_amount_positive"),
            # The columns must tell the same story as the status.
            models.CheckConstraint(
                condition=(
                    Q(status=FundRequestStatus.PENDING, decided_by__isnull=True, decided_at__isnull=True,
                      decision_reason="", paid_by__isnull=True, paid_at__isnull=True, payment_reference="")
                    | Q(status__in=[FundRequestStatus.APPROVED, FundRequestStatus.DECLINED],
                        decided_by__isnull=False, decided_at__isnull=False,
                        paid_by__isnull=True, paid_at__isnull=True, payment_reference="")
                    | (Q(status=FundRequestStatus.PAID, decided_by__isnull=False, decided_at__isnull=False,
                         paid_by__isnull=False, paid_at__isnull=False) & ~Q(payment_reference=""))
                ),
                name="approvals_fundrequest_status_consistent",
            ),
            models.CheckConstraint(
                condition=~Q(status=FundRequestStatus.DECLINED) | ~Q(decision_reason=""),
                name="approvals_fundrequest_decline_has_reason",
            ),
        ]

    def __str__(self):
        return f"{self.tender.reference}: {self.get_purpose_display()} {self.amount} {self.currency} ({self.status})"

    def _invariant_errors(self):
        errors = {}
        if self.amount is None or self.amount <= 0:
            errors["amount"] = "The amount must be greater than zero."
        if self.requested_by_id is None or not _is_active(self.requested_by, Role.PROJECT_MANAGER):
            errors["requested_by"] = "Only a Project Manager can raise a fund request."
        elif self._state.adding:
            if self.requested_by_id != self.tender.project_manager_id:
                errors["requested_by"] = "Only this tender's PM can raise its fund request."
            actual = _fresh_tender_status(self.tender)
            if actual not in FUND_REQUEST_TENDER_STATUSES:
                errors["tender"] = f"A fund request cannot be raised while the tender is {actual}."
        if self.decided_by_id is not None and self.decided_by.role != Role.EXECUTIVE:
            errors["decided_by"] = "Only the Managing Director decides a fund request."
        if self.status == FundRequestStatus.DECLINED and not self.decision_reason.strip():
            errors["decision_reason"] = "A declined fund request must record its reason."
        if self.paid_by_id is not None and self.paid_by.role != Role.FINANCE:
            errors["paid_by"] = "Only Finance marks a fund request paid."
        return errors

    def clean(self):
        super().clean()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._check_guard()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)
        is_new = self._state.adding
        # A new request is the requester's own act, whoever calls save().
        with transaction.atomic(), audit_actor(self.requested_by if is_new else get_current_actor()):
            super().save(*args, **kwargs)
            if is_new:
                _send_on_commit(
                    f"Fund request for {self.tender.reference}: {self.get_purpose_display()}",
                    f"{self.tender.reference} — {self.tender.title or '(untitled)'}\n\n"
                    f"{self.requested_by.get_full_name() or self.requested_by.email} requests "
                    f"{self.amount} {self.currency} ({self.get_purpose_display()}). "
                    "It awaits the Managing Director's approval.\n",
                    _emails(Role.EXECUTIVE),
                )

    # -- transitions ------------------------------------------------------------

    @contextmanager
    def _transition(self, user, *, event, role, allowed_from):
        if not _is_active(user, role):
            raise PermissionDenied(f"{event}: only {Role(role).label} may do this.")
        with transaction.atomic(), audit_actor(user):
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if locked.status != allowed_from:
                raise TransitionError(f"{event} is not allowed from {locked.status} (needs {allowed_from}).")
            yield locked
            with locked._system_write():
                locked.save()
        self.refresh_from_db()

    def _notify(self, subject, line, recipients):
        _send_on_commit(
            f"Fund request for {self.tender.reference}: {subject}",
            f"{self.tender.reference} — {self.tender.title or '(untitled)'}\n\n"
            f"{self.get_purpose_display()}, {self.amount} {self.currency}: {line}\n",
            recipients,
        )

    def approve(self, user, reason=""):
        """EXEC: PENDING → APPROVED, optionally with a reason. Tells the PM, and Finance who must now pay."""
        with self._transition(
            user, event="approve", role=Role.EXECUTIVE, allowed_from=FundRequestStatus.PENDING
        ) as fr:
            fr.status = FundRequestStatus.APPROVED
            fr.decided_by, fr.decided_at = user, timezone.now()
            fr.decision_reason = (reason or "").strip()
            fr._notify("approved", "approved by the Managing Director; Finance to pay.",
                       [fr.tender.project_manager.email, *_emails(Role.FINANCE)])

    def decline(self, user, reason):
        """EXEC: PENDING → DECLINED, with a reason recorded in ``decision_reason``."""
        if not (reason or "").strip():
            raise ValidationError({"decision_reason": "A declined fund request must record its reason."})
        with self._transition(
            user, event="decline", role=Role.EXECUTIVE, allowed_from=FundRequestStatus.PENDING
        ) as fr:
            fr.status = FundRequestStatus.DECLINED
            fr.decided_by, fr.decided_at = user, timezone.now()
            fr.decision_reason = reason.strip()
            fr._notify("declined", f"declined by the Managing Director. Reason: {reason.strip()}",
                       [fr.tender.project_manager.email])

    def mark_paid(self, user, payment_reference):
        """FIN: APPROVED → PAID, with the bank's payment reference. Never touches Tender (R17)."""
        if not (payment_reference or "").strip():
            raise ValidationError({"payment_reference": "Enter the payment reference."})
        with self._transition(
            user, event="mark_paid", role=Role.FINANCE, allowed_from=FundRequestStatus.APPROVED
        ) as fr:
            fr.status = FundRequestStatus.PAID
            fr.paid_by, fr.paid_at = user, timezone.now()
            fr.payment_reference = payment_reference.strip()
            fr._notify("paid", f"paid by Finance (reference {fr.payment_reference}).",
                       [fr.tender.project_manager.email])
