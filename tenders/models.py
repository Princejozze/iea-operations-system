"""
tenders — app 5 in CLAUDE.md §1. Imports only from core and accounts.

Holds Tender, TenderAssignment, TenderOutcome, TenderDocument, TenderCostingRates (R22), and **all
status transition methods** (R19). Lot and TenderExtraction belong here too and
arrive with the PDF intake work.

Reaching downward (§1): the transition methods check records that live in
apps below this one — Item (items), QCAudit (audits), Approval /
EligibilityReview / FundRequest (approvals) — through reverse accessors, never
imports. Those apps must therefore declare these related_names on their
foreign key to Tender:

    items.Item.tender               related_name="items"
    items.ItemAttachment.item       related_name="attachments"   (field: kind)
    audits.QCAudit.tender           related_name="qc_audits"     (field: auditor)
    approvals.Approval.tender       related_name="approvals"
    approvals.EligibilityReview     related_name="eligibility_reviews"
    approvals.FundRequest.tender    related_name="fund_requests"  (fields: purpose, status)

Every gate that depends on one of them **fails closed**: if the app is not
installed yet, the accessor does not exist and the transition is refused.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.utils import timezone

from accounts.models import Role
from core.audit import audit_actor
from core.models import AuditAction, AuditEntry, Auditable, AuditableQuerySet, CostingDefaults, CostingRates
from core.references import next_tender_reference

# ---------------------------------------------------------------------------
# Vocabularies (§8)
# ---------------------------------------------------------------------------


class TenderStatus(models.TextChoices):
    EXTRACTING = "EXTRACTING", "Extracting"
    PENDING_VERIFICATION = "PENDING_VERIFICATION", "Pending verification"
    PENDING_REGISTRATION_APPROVAL = "PENDING_REGISTRATION_APPROVAL", "Pending registration approval"
    DRAFTING = "DRAFTING", "Drafting"
    COSTING = "COSTING", "Costing"
    PENDING_AUDIT = "PENDING_AUDIT", "Pending audit"
    PENDING_EXECUTIVE_APPROVAL = "PENDING_EXECUTIVE_APPROVAL", "Pending executive approval"
    READY_TO_SUBMIT = "READY_TO_SUBMIT", "Ready to submit"
    SUBMITTED = "SUBMITTED", "Submitted"
    WON = "WON", "Won"
    LOST = "LOST", "Lost"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"
    PENDING_ELIGIBILITY_DECISION = "PENDING_ELIGIBILITY_DECISION", "Pending eligibility decision"


S = TenderStatus

# §8: WITHDRAWN is reachable from any state before SUBMITTED.
WITHDRAWABLE_STATUSES = (
    S.EXTRACTING,
    S.PENDING_VERIFICATION,
    S.PENDING_REGISTRATION_APPROVAL,
    S.DRAFTING,
    S.COSTING,
    S.PENDING_ELIGIBILITY_DECISION,
    S.PENDING_AUDIT,
    S.PENDING_EXECUTIVE_APPROVAL,
    S.READY_TO_SUBMIT,
)
# §8: the eligibility side state is enterable only from these.
ELIGIBILITY_ENTRY_STATUSES = (S.DRAFTING, S.COSTING)
# R4: statuses in which exchange_rate_locked_at may be set.
RATE_LOCKED_STATUSES = (
    S.PENDING_AUDIT,
    S.PENDING_EXECUTIVE_APPROVAL,
    S.READY_TO_SUBMIT,
    S.SUBMITTED,
    S.WON,
    S.LOST,
    S.WITHDRAWN,
)
# R15: item, costing and RFQ work is allowed only once registered.
WORK_ALLOWED_STATUSES = (S.DRAFTING, S.COSTING)
# R17: only a PAID FundRequest with this purpose counts as NeST funds. The value
# must equal approvals.FundRequestPurpose.NEST_APPLICATION_FEE (a test checks it;
# tenders cannot import approvals, §1).
NEST_FUNDS_PURPOSE = "NEST_APPLICATION_FEE"


class BidOpeningPosition(models.TextChoices):
    LOWEST = "LOWEST", "Lowest"
    SECOND_LOWEST = "SECOND_LOWEST", "Second lowest"
    THIRD_LOWEST = "THIRD_LOWEST", "Third lowest"
    FOURTH_LOWEST = "FOURTH_LOWEST", "Fourth lowest"
    FIFTH_LOWEST = "FIFTH_LOWEST", "Fifth lowest"
    ABOVE_FIFTH = "ABOVE_FIFTH", "Above fifth"
    HIGHEST = "HIGHEST", "Highest"
    NOT_SUBMITTED = "NOT_SUBMITTED", "Not submitted"


class AwardStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    AWARDED = "AWARDED", "Awarded"
    NOT_AWARDED = "NOT_AWARDED", "Not awarded"
    CANCELLED = "CANCELLED", "Cancelled"


class BidSecurityType(models.TextChoices):
    """
    §8. Taken from the real distribution across 336 legacy tenders — not free
    text. There is no separate "bid security required" flag: NOT_APPLICABLE is
    a value of this field, so the two can never disagree.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"  # 142 historic
    TENDER_SECURING_DECLARATION = "TENDER_SECURING_DECLARATION", "Tender securing declaration"  # 77
    BANK_LETTER_PROOF_OF_FUNDS = "BANK_LETTER_PROOF_OF_FUNDS", "Bank letter / proof of funds"  # 62
    BANK_GUARANTEE = "BANK_GUARANTEE", "Bank guarantee"  # 34
    INSURANCE_BOND = "INSURANCE_BOND", "Insurance bond"


class Currency(models.TextChoices):
    USD = "USD", "USD"
    TZS = "TZS", "TZS"
    GBP = "GBP", "GBP"
    EUR = "EUR", "EUR"
    ZAR = "ZAR", "ZAR"


# R2: Item fields required before QC audit (§7), by kind of emptiness check.
R2_REQUIRED_VALUE_FIELDS = ("unit_cost", "supplier")  # null = missing
R2_REQUIRED_TEXT_FIELDS = (  # null or "" = missing
    "currency",
    "incoterm",
    "unit_of_measure",
    "category",
    "make_brand",
    "model_part_number",
    "country_of_origin",
)
R2_REQUIRED_ATTACHMENT_KINDS = ("BROCHURE", "QUOTATION", "DATASHEET")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TransitionError(ValidationError):
    """A status transition was refused because a rule (R-number) forbids it."""


class GuardedFieldError(ValidationError):
    """A system-set field was written outside a transition method (R19, R13)."""


# ---------------------------------------------------------------------------
# Tender
# ---------------------------------------------------------------------------


class TenderQuerySet(AuditableQuerySet):
    pass


class TenderManager(models.Manager.from_queryset(TenderQuerySet)):
    def create_from_upload(self, *, uploaded_by, pdf=None):
        """
        §5 steps 2-3: the only way a tender is created. Gives it its reference
        (R20) in the same transaction, status EXTRACTING, and the uploading PM
        as its project manager.

        ``pdf`` is the uploaded file (a Django File). When given it is stored as the
        tender's TENDER_PDF document in the same transaction, and nothing else happens
        in the request: conversion and extraction run later, from the cron-driven
        command (§6 "Extraction runs asynchronously"). It must look like a PDF.
        """
        if not (uploaded_by.is_active and uploaded_by.role == Role.PROJECT_MANAGER):
            raise PermissionDenied("Only a Project Manager can register a tender by upload.")
        if pdf is not None:
            head = pdf.read(5)
            pdf.seek(0)
            if head != b"%PDF-":
                raise ValidationError("That file is not a PDF.")
        with transaction.atomic(), audit_actor(uploaded_by):
            tender = self.model(created_by=uploaded_by, project_manager=uploaded_by)
            with tender._system_write():
                tender.save()
            TenderAssignment.objects.create(
                tender=tender, from_pm=None, to_pm=uploaded_by, assigned_by=uploaded_by,
                reason="Registered by upload.",
            )
            TenderCostingRates.create_for(tender)  # R22: the bid's own rates, from the company baseline
            if pdf is not None:
                TenderDocument.objects.create(
                    tender=tender, kind=TenderDocumentKind.TENDER_PDF, file=pdf,
                    original_filename=getattr(pdf, "name", "") or "", uploaded_by=uploaded_by,
                )
        return tender


class Tender(Auditable):
    # -- system-set (§7) --------------------------------------------------------
    reference = models.CharField(max_length=20, unique=True, editable=False)
    legacy_reference = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="Original KEA-prefixed reference on imported records. Never used for new tenders.",
    )
    status = models.CharField(
        max_length=32, choices=TenderStatus.choices, default=S.EXTRACTING, editable=False
    )
    previous_status = models.CharField(
        max_length=32, choices=TenderStatus.choices, blank=True, editable=False,
        help_text="Where PENDING_ELIGIBILITY_DECISION returns to. Empty otherwise.",
    )
    exchange_rate_locked_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tenders_created",
        editable=False,
    )

    # -- PM-confirmed (§7). Blank until the PM confirms extraction. -------------
    title = models.CharField(max_length=500, blank=True)
    client = models.ForeignKey(
        "core.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="tenders"
    )
    client_address = models.TextField(blank=True)
    official_reference = models.CharField(
        max_length=100, blank=True, db_index=True, help_text="The tender number published on NeST."
    )
    submission_deadline = models.DateTimeField(null=True, blank=True)
    clarification_deadline = models.DateTimeField(null=True, blank=True)
    # Empty = opened at the submission deadline, as PPRA standard documents provide. Set it only
    # when the client states a different opening. Not copied from the deadline on save: a copy
    # would go stale when the deadline is extended. Read it through ``opening_at``.
    bid_opening_date = models.DateTimeField(
        null=True, blank=True,
        help_text="When bids are publicly opened. Leave empty if at the submission deadline.",
    )
    # Blank = not yet verified by the PM. NOT_APPLICABLE is a real answer (§8).
    bid_security_type = models.CharField(
        max_length=32, choices=BidSecurityType.choices, blank=True
    )
    site_visit_required = models.BooleanField(null=True, blank=True)
    site_visit_date = models.DateTimeField(null=True, blank=True)
    special_requirements = models.TextField(blank=True, help_text="One requirement per line.")
    required_forms = models.TextField(blank=True, help_text="One form per line.")
    category = models.ForeignKey(
        "core.TenderCategory", on_delete=models.PROTECT, null=True, blank=True, related_name="tenders"
    )

    # -- assignment (R13) -----------------------------------------------------------
    project_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tenders_managed",
        editable=False,
    )

    objects = TenderManager()

    # Written only inside _system_write(), i.e. by create_from_upload, the
    # transition methods (R19) and reassign() (R13).
    GUARDED_FIELDS = (
        "reference", "status", "previous_status", "exchange_rate_locked_at",
        "created_by", "project_manager",
    )
    # Fields the PM confirms in confirm_extraction (R12).
    CONFIRMED_FIELDS = (
        "title", "client", "client_address", "official_reference", "submission_deadline",
        "clarification_deadline", "bid_security_type",
        "site_visit_required", "site_visit_date", "special_requirements", "required_forms",
        "category",
    )
    REQUIRED_AT_CONFIRMATION = (
        "title", "client", "official_reference", "submission_deadline",
        "bid_security_type", "site_visit_required",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=TenderStatus.values), name="tenders_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(reference__regex=r"^IEA-[0-9]{4}-[0-9]{4}$"),
                name="tenders_reference_format",
            ),
            # previous_status is set exactly while in the eligibility side state.
            models.CheckConstraint(
                condition=(
                    Q(status=S.PENDING_ELIGIBILITY_DECISION, previous_status__in=ELIGIBILITY_ENTRY_STATUSES)
                    | (~Q(status=S.PENDING_ELIGIBILITY_DECISION) & Q(previous_status=""))
                ),
                name="tenders_previous_status_only_in_eligibility",
            ),
            # R4: the rate lock exists only from PENDING_AUDIT onwards.
            models.CheckConstraint(
                condition=Q(exchange_rate_locked_at__isnull=True) | Q(status__in=RATE_LOCKED_STATUSES),
                name="tenders_rate_lock_only_after_audit_entry",
            ),
            models.CheckConstraint(
                condition=(
                    Q(clarification_deadline__isnull=True)
                    | Q(submission_deadline__isnull=True)
                    | Q(clarification_deadline__lte=F("submission_deadline"))
                ),
                name="tenders_clarification_before_submission",
            ),
            models.CheckConstraint(
                condition=(
                    Q(bid_opening_date__isnull=True)
                    | Q(submission_deadline__isnull=True)
                    | Q(bid_opening_date__gte=F("submission_deadline"))
                ),
                name="tenders_bid_opening_not_before_submission",
            ),
            models.CheckConstraint(
                condition=Q(bid_security_type__in=["", *BidSecurityType.values]),
                name="tenders_bid_security_type_valid",
            ),
        ]

    def __str__(self):
        return f"{self.reference} — {self.title or '(awaiting verification)'}"

    # -- guarded writes ---------------------------------------------------------

    @contextmanager
    def _system_write(self):
        self._system_write_allowed = True
        try:
            yield
        finally:
            self._system_write_allowed = False

    def _guarded_field_errors(self):
        if getattr(self, "_system_write_allowed", False):
            return {}
        if self._state.adding:
            return {"status": "A tender can only be created by upload (Tender.objects.create_from_upload)."}
        attnames = [self._meta.get_field(f).attname for f in self.GUARDED_FIELDS]
        stored = type(self)._base_manager.filter(pk=self.pk).values(*attnames).first()
        if stored is None:
            return {}
        return {
            name: f"{name} is system-set and changes only through a transition method (R19/R13)."
            for name, attname in zip(self.GUARDED_FIELDS, attnames)
            if stored[attname] != getattr(self, attname)
        }

    # -- invariants: clean() and save() (§4 Enforcement) --------------------------

    def _invariant_errors(self):
        errors = {}
        pm = self.project_manager if self.project_manager_id else None
        if pm is None or pm.role != Role.PROJECT_MANAGER:
            errors["project_manager"] = "Only a user with role PM may hold a tender (§2, R13)."
        if (
            self.clarification_deadline and self.submission_deadline
            and self.clarification_deadline > self.submission_deadline
        ):
            errors["clarification_deadline"] = "The clarification deadline must not be after the submission deadline."
        if self.bid_opening_date and self.submission_deadline and self.bid_opening_date < self.submission_deadline:
            errors["bid_opening_date"] = "Bids cannot be opened before the submission deadline."
        return errors

    def clean(self):
        super().clean()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        guarded = self._guarded_field_errors()
        if guarded:
            raise GuardedFieldError(guarded)
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)
        with transaction.atomic():
            if self._state.adding and not self.reference:
                self.reference = next_tender_reference()  # R20, same transaction
            super().save(*args, **kwargs)

    # -- derived state ------------------------------------------------------------

    def _related(self, accessor):
        """A reverse accessor from an app below this one, or None if that app
        is not installed (callers must then fail closed)."""
        return getattr(self, accessor, None)

    @property
    def has_nest_funds(self):
        """R17: true only when a NEST_APPLICATION_FEE FundRequest for this tender is PAID.

        A paid BID_SECURITY, TENDER_DOCUMENT_FEE or OTHER request does not count.
        """
        fund_requests = self._related("fund_requests")
        return fund_requests is not None and fund_requests.filter(
            purpose=NEST_FUNDS_PURPOSE, status="PAID"
        ).exists()

    @property
    def opening_at(self):
        """When bids are opened: ``bid_opening_date`` if set, else the submission deadline."""
        return self.bid_opening_date or self.submission_deadline

    @property
    def is_frozen(self):
        """R9a: awaiting the MD's eligibility decision. Nothing but that decision moves it."""
        return self.status == S.PENDING_ELIGIBILITY_DECISION

    def require_unfrozen(self, action):
        """R9a guard for apps below tenders (items, audits): item edits, pricing,
        RFQs and audits call this first. Reads the stored status, not a cached one."""
        stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
        if stored == S.PENDING_ELIGIBILITY_DECISION:
            raise TransitionError(
                f"{action} is refused: the tender is frozen awaiting the MD's eligibility decision (R9a).",
                code="r9a",
            )

    @property
    def work_allowed(self):
        """R15: item, costing and RFQ work may happen only after registration."""
        return self.status in WORK_ALLOWED_STATUSES

    # -- transition machinery (R19) -------------------------------------------------

    @contextmanager
    def _transition(self, user, *, event, allowed_from, roles=(), must_be_pm=False):
        """
        Shared shape of every transition method (R19):
          1. check who is acting (role, or "this tender's PM");
          2. lock the row and re-read the status from the database, so two
             people acting at once cannot both pass the check;
          3. hand the locked copy to the method body, which applies its rules
             and sets the new status — raising refuses the whole transition;
          4. save it inside a system write: one STATUS_CHANGE AuditEntry
             attributed to ``user``, in the same transaction;
          5. refresh this instance from the database.
        """
        if user is not None and not user.is_active:
            raise PermissionDenied(f"{event}: inactive users cannot act on tenders.")
        if roles and (user is None or user.role not in roles):
            labels = ", ".join(Role(r).label for r in roles)
            raise PermissionDenied(f"{event}: only {labels} may do this.")
        with transaction.atomic(), audit_actor(user):
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if must_be_pm and (user is None or locked.project_manager_id != user.pk):
                raise PermissionDenied(f"{event}: only this tender's PM may do this.")
            if locked.status not in allowed_from:
                raise TransitionError(
                    f"{event} is not allowed from {locked.status} "
                    f"(allowed from: {', '.join(allowed_from)}).",
                    code="r19",
                )
            yield locked
            with locked._system_write():
                locked.save()
        self.refresh_from_db()

    # Each method below is named for its event (R19), takes the acting user,
    # checks its rules, and is the only writer of the status it sets.

    def finish_extraction(self, user=None):
        """System: extraction ended (success, partial or failure) → PM verifies (§5.6, R12).

        Not in R19's list, which has no event for this system step; named here
        so that no code outside a transition method ever writes status.
        """
        with self._transition(user, event="finish_extraction", allowed_from=(S.EXTRACTING,)) as t:
            t.status = S.PENDING_VERIFICATION

    def confirm_extraction(self, user, **verified):
        """PM confirms every verified field → MD registration approval (§5.7, R12)."""
        unknown = set(verified) - set(self.CONFIRMED_FIELDS)
        if unknown:
            raise TypeError(f"Not PM-confirmable fields: {sorted(unknown)}")
        with self._transition(
            user, event="confirm_extraction", allowed_from=(S.PENDING_VERIFICATION,), must_be_pm=True
        ) as t:
            for name, value in verified.items():
                setattr(t, name, value)
            missing = [
                name for name in self.REQUIRED_AT_CONFIRMATION
                if getattr(t, self._meta.get_field(name).attname) in (None, "")
            ]
            if missing:
                raise TransitionError({name: "Required before the tender can go to the MD." for name in missing})
            t.full_clean(exclude=["reference", "created_by", "project_manager"])
            t.status = S.PENDING_REGISTRATION_APPROVAL

    def approve_registration(self, user):
        """R15: the MD's registration decision. APPROVED → DRAFTING, DECLINED → WITHDRAWN.

        Requires an Approval of kind REGISTRATION decided by this EXEC (created
        by the approvals app, with signature). Fails closed without it.
        """
        with self._transition(
            user, event="approve_registration",
            allowed_from=(S.PENDING_REGISTRATION_APPROVAL,), roles=(Role.EXECUTIVE,),
        ) as t:
            approval = t._latest_approval("REGISTRATION", user, rule="R15")
            approved = approval.decision == "APPROVED"
            t.status = S.DRAFTING if approved else S.WITHDRAWN
            t._notify_pm_on_commit(
                user,
                "registration approved — work may begin" if approved
                else "registration declined — the tender is withdrawn",
                reason=approval.comments,
            )

    def start_costing(self, user):
        """PM starts costing (§5.12). Only reachable after registration (R15).

        Requires at least one Item and nothing more (§5): not that the
        specification split succeeded, never that an AI step worked (R12). Fails
        closed if the items app is not installed.
        """
        with self._transition(
            user, event="start_costing", allowed_from=(S.DRAFTING,), must_be_pm=True
        ) as t:
            items = t._related("items")
            if items is None or not items.exists():
                raise TransitionError(
                    "Add at least one item before costing starts (§5): an item sheet cannot be costed empty.",
                    code="no_items",
                )
            t.status = S.COSTING

    def submit_for_audit(self, user):
        """PM submits the item sheet to QC audit. R2 completeness gate; R4 rate lock."""
        with self._transition(
            user, event="submit_for_audit", allowed_from=(S.COSTING,), must_be_pm=True
        ) as t:
            blockers = t.r2_blockers()
            if blockers:
                raise TransitionError("; ".join(blockers), code="r2")
            t.status = S.PENDING_AUDIT
            t.exchange_rate_locked_at = timezone.now()

    def record_audit_result(self, user, *, compliant):
        """Auditor's verdict. Compliant → MD; non-compliant → back to COSTING (R9), lock cleared (R4).

        R1 is checked here on the real foreign keys, and a QCAudit by this
        auditor must exist (audits app). Fails closed without it.
        """
        with self._transition(
            user, event="record_audit_result", allowed_from=(S.PENDING_AUDIT,), roles=(Role.AUDITOR,)
        ) as t:
            user.assert_can_audit(t)  # R1
            qc_audits = t._related("qc_audits")
            if qc_audits is None or not qc_audits.filter(auditor=user).exists():
                raise TransitionError("No QC audit by this auditor is recorded for this tender.", code="r9")
            if compliant:
                t.status = S.PENDING_EXECUTIVE_APPROVAL
            else:
                t.status = S.COSTING
                t.exchange_rate_locked_at = None

    def approve_submission(self, user):
        """The MD's submission decision. Needs a fresh SUBMISSION Approval by this EXEC.

        APPROVED → READY_TO_SUBMIT (R3: no other route gets there).
        DECLINED → back to COSTING, never a dead end (R9): the rate lock is
        cleared (R4), the PM is notified, and the reason — the Approval's
        comments, required on a decline — is in the notice and the audit trail.
        """
        with self._transition(
            user, event="approve_submission",
            allowed_from=(S.PENDING_EXECUTIVE_APPROVAL,), roles=(Role.EXECUTIVE,),
        ) as t:
            approval = t._latest_approval("SUBMISSION", user, rule="R3")
            if approval.decision == "APPROVED":
                t.status = S.READY_TO_SUBMIT
                t._notify_pm_on_commit(user, "submission approved — ready to submit once NeST is funded")
            elif approval.decision == "DECLINED":
                if not approval.comments.strip():
                    raise TransitionError("A declined submission must record its reason (R9).", code="r9")
                t.status = S.COSTING
                t.exchange_rate_locked_at = None
                t._notify_pm_on_commit(
                    user, "submission declined — returned to COSTING", reason=approval.comments
                )
            else:
                raise TransitionError(f"Unknown approval decision {approval.decision!r} (R3).", code="r3")

    def mark_submitted(self, user):
        """PM records the bid as uploaded to NeST. R17 funds gate."""
        with self._transition(
            user, event="mark_submitted", allowed_from=(S.READY_TO_SUBMIT,), must_be_pm=True
        ) as t:
            if not t.has_nest_funds:
                raise TransitionError("The NeST account is not funded: no PAID fund request (R17).", code="r17")
            t.status = S.SUBMITTED

    def record_outcome(self, user, *, award_status, bid_opening_position="", contract_sum=None,
                       contract_currency=""):
        """Capture position at opening and award result (§8 AwardStatus).

        AWARDED → WON; NOT_AWARDED or CANCELLED → LOST; PENDING stays SUBMITTED.
        """
        if award_status not in AwardStatus.values:
            raise TransitionError(f"Unknown award status {award_status!r}.")
        with self._transition(
            user, event="record_outcome", allowed_from=(S.SUBMITTED,),
            roles=(Role.PROJECT_MANAGER, Role.EXECUTIVE),
        ) as t:
            if user.role == Role.PROJECT_MANAGER and t.project_manager_id != user.pk:
                raise PermissionDenied("Only this tender's PM or the MD may record its outcome.")
            outcome, _ = TenderOutcome.objects.get_or_create(tender=t, defaults={"recorded_by": user})
            outcome.award_status = award_status
            outcome.bid_opening_position = bid_opening_position
            outcome.contract_sum = contract_sum
            outcome.contract_currency = contract_currency
            outcome.recorded_by = user
            outcome.full_clean()
            outcome.save()
            if award_status == AwardStatus.AWARDED:
                t.status = S.WON
            elif award_status in (AwardStatus.NOT_AWARDED, AwardStatus.CANCELLED):
                t.status = S.LOST

    def raise_eligibility(self, user):
        """R16: PM escalates an unmet special requirement → PENDING_ELIGIBILITY_DECISION.

        Requires an open (undecided) EligibilityReview raised by this PM.
        """
        with self._transition(
            user, event="raise_eligibility", allowed_from=ELIGIBILITY_ENTRY_STATUSES, must_be_pm=True
        ) as t:
            reviews = t._related("eligibility_reviews")
            if reviews is None or not reviews.filter(raised_by=user, md_decision="").exists():
                raise TransitionError("Raise an EligibilityReview first (R16).", code="r16")
            t.previous_status = t.status
            t.status = S.PENDING_ELIGIBILITY_DECISION

    def resolve_eligibility(self, user):
        """R16: only the MD resolves. WITHDRAW → WITHDRAWN; otherwise back to previous_status."""
        with self._transition(
            user, event="resolve_eligibility",
            allowed_from=(S.PENDING_ELIGIBILITY_DECISION,), roles=(Role.EXECUTIVE,),
        ) as t:
            reviews = t._related("eligibility_reviews")
            if reviews is not None and reviews.filter(md_decision="").exists():
                raise TransitionError("An eligibility review is still undecided (R16).", code="r16")
            review = (
                reviews.filter(decided_by=user).exclude(md_decision="").order_by("-decided_at").first()
                if reviews is not None else None
            )
            # The decision must be from this freeze, not an earlier escalation.
            if review is None or review.decided_at < t._entered_status_at():
                raise TransitionError("No eligibility decision by the MD is recorded (R16).", code="r16")
            withdrawn = review.md_decision == "WITHDRAW"
            t.status = S.WITHDRAWN if withdrawn else t.previous_status
            t.previous_status = ""
            t._notify_pm_on_commit(
                user,
                f"eligibility decision: {review.md_decision.replace('_', ' ').lower()}"
                + (" — the tender is withdrawn" if withdrawn else f" — back to {t.status}"),
                reason=review.decision_notes,
            )

    def withdraw(self, user):
        """§8: EXEC only, from any state before SUBMITTED."""
        with self._transition(
            user, event="withdraw", allowed_from=WITHDRAWABLE_STATUSES, roles=(Role.EXECUTIVE,)
        ) as t:
            t.status = S.WITHDRAWN
            t.previous_status = ""

    # -- R13 ----------------------------------------------------------------------------

    def reassign(self, user, new_pm, *, reason=""):
        """R13: only EXEC reassigns, only to an active PM. Writes a
        TenderAssignment and notifies both PMs once the change commits."""
        if user is None or not user.is_active or user.role != Role.EXECUTIVE:
            raise PermissionDenied("Only the Managing Director can reassign a tender (R13).")
        if not new_pm.is_active or new_pm.role != Role.PROJECT_MANAGER:
            raise ValidationError("A tender can only be assigned to an active Project Manager (R13).")
        with transaction.atomic(), audit_actor(user):
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            old_pm = locked.project_manager
            if old_pm.pk == new_pm.pk:
                raise ValidationError(f"{new_pm} already holds this tender.")
            locked.project_manager = new_pm
            with locked._system_write():
                locked.save()
            TenderAssignment.objects.create(
                tender=locked, from_pm=old_pm, to_pm=new_pm, assigned_by=user, reason=reason
            )
            transaction.on_commit(
                lambda: _notify_reassignment(locked, old_pm=old_pm, new_pm=new_pm, by=user)
            )
        self.refresh_from_db()

    # -- helpers for gates ----------------------------------------------------------------

    def _entered_status_at(self):
        """When the tender last entered its current status, from the audit trail.

        An MD decision only counts if it was made after this moment: otherwise
        a DECLINED approval from an earlier round would bounce the tender again,
        and an old decision could stand in for a new one.
        """
        entry = (
            AuditEntry.objects.filter(
                model="tenders.Tender", object_id=str(self.pk), action=AuditAction.STATUS_CHANGE
            )
            .order_by("-timestamp", "-id")
            .first()
        )
        return entry.timestamp if entry is not None else self.created_at

    def _latest_approval(self, kind, user, *, rule):
        approvals = self._related("approvals")
        approval = (
            approvals.filter(
                kind=kind, decided_by=user, decided_by__role=Role.EXECUTIVE,
                decided_at__gte=self._entered_status_at(),
            )
            .order_by("-decided_at", "-id").first()
            if approvals is not None else None
        )
        if approval is None:
            raise TransitionError(
                f"No {kind} Approval by the MD is recorded for this tender ({rule}).", code=rule.lower()
            )
        return approval

    def _notify_pm_on_commit(self, by, outcome, *, reason=""):
        """§10: the PM is told of every MD decision. Sent once the transition commits."""
        pm, reference, title = self.project_manager, self.reference, self.title or "(untitled)"
        body = (
            f"{reference} — {title}\n\n"
            f"The Managing Director ({by.get_full_name() or by.email}): {outcome}.\n"
        )
        if reason.strip():
            body += f"\nReason: {reason.strip()}\n"
        subject = f"Tender {reference}: {outcome.split(' — ')[0]}"
        transaction.on_commit(
            lambda: send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [pm.email])
        )

    def r2_blockers(self):
        """R2: reasons this tender cannot go to QC audit. Empty list = ready."""
        items = self._related("items")
        if items is None:
            return ["The item sheet is not available — items app not installed (R2)"]
        if not items.exists():
            return ["The tender has no items (R2)"]
        problems = []
        for name in R2_REQUIRED_VALUE_FIELDS:
            n = items.filter(**{f"{name}__isnull": True}).count()
            if n:
                problems.append(f"{n} item(s) missing {name}")
        for name in R2_REQUIRED_TEXT_FIELDS:
            n = items.filter(Q(**{f"{name}__isnull": True}) | Q(**{name: ""})).count()
            if n:
                problems.append(f"{n} item(s) missing {name}")
        for kind in R2_REQUIRED_ATTACHMENT_KINDS:
            n = items.exclude(attachments__kind=kind).count()
            if n:
                problems.append(f"{n} item(s) missing a {kind.lower()} attachment")
        return [f"{p} (R2)" for p in problems]


def _notify_reassignment(tender, *, old_pm, new_pm, by):
    subject = f"Tender {tender.reference} reassigned"
    body = (
        f"{tender.reference} — {tender.title or '(awaiting verification)'}\n\n"
        f"The Managing Director ({by.get_full_name() or by.email}) has reassigned this tender "
        f"from {old_pm.get_full_name() or old_pm.email} to {new_pm.get_full_name() or new_pm.email}.\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [old_pm.email, new_pm.email])


# ---------------------------------------------------------------------------
# TenderAssignment — R13 history
# ---------------------------------------------------------------------------


class TenderAssignment(Auditable):
    """One row per change of PM: who held it, who holds it now, who moved it."""

    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="assignments")
    from_pm = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="Empty for the first assignment at upload.",
    )
    to_pm = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.tender.reference}: {self.from_pm or '—'} → {self.to_pm}"


# ---------------------------------------------------------------------------
# TenderOutcome — position at opening, award result
# ---------------------------------------------------------------------------


class TenderOutcome(Auditable):
    tender = models.OneToOneField(Tender, on_delete=models.PROTECT, related_name="outcome")
    bid_opening_position = models.CharField(max_length=16, choices=BidOpeningPosition.choices, blank=True)
    award_status = models.CharField(max_length=16, choices=AwardStatus.choices, default=AwardStatus.PENDING)
    contract_sum = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    contract_currency = models.CharField(max_length=3, choices=Currency.choices, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(contract_sum__isnull=True) | ~Q(contract_currency=""),
                name="tenders_outcome_contract_sum_has_currency",
            ),
        ]

    def __str__(self):
        return f"{self.tender.reference}: {self.get_award_status_display()}"


# ---------------------------------------------------------------------------
# TenderDocument — §7: the files that belong to a tender
# ---------------------------------------------------------------------------


class TenderDocumentKind(models.TextChoices):
    TENDER_PDF = "TENDER_PDF", "Tender PDF"  # the NeST original; also carries `markdown`
    TECHNICAL_SPEC = "TECHNICAL_SPEC", "Technical specification"  # split out as the RFQ reference
    SUPPORTING = "SUPPORTING", "Supporting document"


class TenderDocument(Auditable):
    """A file attached to a tender. ``kind`` says which: the published tender
    PDF (whose converted ``markdown`` is stored on this row, §6), the technical
    specification split out of it, or any supporting file."""

    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="documents")
    kind = models.CharField(max_length=16, choices=TenderDocumentKind.choices)
    file = models.FileField(upload_to="tender_documents/%Y/%m/")
    original_filename = models.CharField(max_length=255, blank=True)
    markdown = models.TextField(blank=True, help_text="PDF→Markdown with page markers (§6). TENDER_PDF rows.")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    # The converted Markdown is ~230,000 characters for a 173-page tender: record THAT it changed
    # in the audit trail (R7), not a copy of it.
    audit_redact = ("markdown",)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=TenderDocumentKind.values), name="tenders_tenderdocument_kind_valid"
            ),
        ]

    def __str__(self):
        return f"{self.tender.reference}: {self.get_kind_display()} ({self.original_filename or self.file.name})"


# ---------------------------------------------------------------------------
# TenderExtraction — §6/§7: what the pipeline worked out about a tender's PDF
# ---------------------------------------------------------------------------


class ExtractionOutcome(models.TextChoices):
    PROCESSING = "PROCESSING", "Processing"  # claimed by a worker; converting
    PREPARED = "PREPARED", "Prepared"  # converted and sectioned; sections are ready to send
    READING = "READING", "Reading"  # claimed by a worker; the model is being asked
    EXTRACTED = "EXTRACTED", "Extracted"  # the model's answer passed the guard and the core fields are present
    PARTIAL = "PARTIAL", "Partly extracted"  # something was read, but not every core field
    FAILED = "FAILED", "Extraction failed"  # nothing usable: the PM types the tender (R12)
    MANUAL_ENTRY = "MANUAL_ENTRY", "Manual entry"  # nothing readable to send: the PM types the tender (R12)


# Where an extraction ends. Every one of these moves the tender on to PENDING_VERIFICATION (§5 step 6).
FINISHED_OUTCOMES = (
    ExtractionOutcome.EXTRACTED, ExtractionOutcome.PARTIAL, ExtractionOutcome.FAILED, ExtractionOutcome.MANUAL_ENTRY,
)


class TenderExtraction(Auditable):
    """
    One row per tender: what the pipeline worked out about its PDF, and what the model read from it.

    ``plan`` records the sections located (§6). ``fields`` holds the extracted values, each with a
    ``confidence`` and ``source_page`` so the PM can check it against the page; they land HERE and
    NEVER directly on Tender (R12): the PM verifies and confirm_extraction copies them across.
    ``fields`` is empty when the outcome is FAILED or MANUAL_ENTRY — the PM types everything.
    """

    tender = models.OneToOneField(Tender, on_delete=models.PROTECT, related_name="extraction")
    outcome = models.CharField(max_length=16, choices=ExtractionOutcome.choices, default=ExtractionOutcome.PROCESSING)
    message = models.TextField(blank=True, help_text="Shown to the PM, e.g. why the tender needs manual entry.")
    converter = models.CharField(max_length=100, blank=True)
    plan = models.JSONField(default=dict, blank=True, help_text="Sections located, fallbacks, raw-text pages. See tenders.pipeline.sections.")
    fields = models.JSONField(
        default=dict, blank=True,
        help_text="{field: {value, confidence, source_page, quote, flags, low_confidence, source}}. Unverified: R12.",
    )
    raw_output = models.TextField(blank=True, help_text="The model's answer exactly as returned, for audit and debugging.")
    usage = models.JSONField(default=dict, blank=True, help_text="Token counts for this document's model call (§6 budget).")
    model_name = models.CharField(max_length=100, blank=True, help_text="The extractor and model that produced `fields`.")
    extracted_at = models.DateTimeField(null=True, blank=True)

    audit_redact = ("plan", "raw_output")  # bulky, and recoverable from the PDF and the model call

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(outcome__in=ExtractionOutcome.values), name="tenders_extraction_outcome_valid"
            ),
        ]

    def __str__(self):
        return f"{self.tender.reference}: extraction {self.outcome}"


# ---------------------------------------------------------------------------
# TenderCostingRates — R22: the MD's per-bid estimate parameters
# ---------------------------------------------------------------------------

# R22: the MD may tune a bid's rates only while its items may be priced. Leaving COSTING
# locks them, alongside the R4 exchange-rate lock; R9 returning the tender to COSTING
# unlocks them. PENDING_ELIGIBILITY_DECISION is outside this set, so R9a freezes them too.
RATES_EDITABLE_STATUSES = WORK_ALLOWED_STATUSES  # DRAFTING, COSTING


def _rate_percent_field(help_text):
    return models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=f"Percent, e.g. 10 means 10%. {help_text}",
    )


def _stored_status(tender_id):
    return Tender._base_manager.filter(pk=tender_id).values_list("status", flat=True).first()


@dataclass(frozen=True)
class BidPreview:
    """What the bid would come to at a candidate rate set. Nothing was saved."""

    total_selling: Decimal  # sum of the priced items' total_selling, in TZS, ex VAT (§4)
    priced_items: int
    unpriced_items: int  # not in the total: they have no price to cost yet


@dataclass(frozen=True)
class RatesApplied:
    """The outcome of TenderCostingRates.apply()."""

    old_rates: dict  # field -> value: percents, labour in TZS per hour
    new_rates: dict
    old_total: Decimal  # the bid total (ex VAT) before and after re-costing
    new_total: Decimal
    items_recosted: int


class TenderCostingRatesQuerySet(AuditableQuerySet):
    def delete(self):
        raise ValidationError("A tender's costing rates cannot be deleted (R22).")


class TenderCostingRates(Auditable):
    """
    The estimate parameters for ONE bid (R22). ``CostingDefaults`` is the company
    baseline; this row is copied from it when the tender is created, and from then on
    the costing chain prices this tender's items at these rates, not the defaults.

    The MD tunes them to find a competitive price: ``preview()`` returns the bid total at
    candidate values without saving anything, and ``apply()`` sets them and re-costs every
    item. Only EXEC may apply; every change is audited (R7). They are editable only while
    the tender is DRAFTING or COSTING, and lock when it leaves COSTING (with R4).

    The formulas never change here (§4), only the numbers. VAT is not a per-bid lever: it
    is statutory, applied at bid-summary level, and still comes from CostingDefaults.
    """

    RATE_FIELDS = (
        "shipping_pct", "duty_pct", "cnf_pct", "labour_rate_tzs_per_hour", "overhead_pct",
        "margin_pct", "maintenance_pct", "cost_of_capital_pct",
    )
    # percent field -> CostingRates fraction field
    FRACTION_OF = {
        "shipping_pct": "shipping_rate", "duty_pct": "duty_rate", "cnf_pct": "cnf_rate",
        "overhead_pct": "overhead_rate", "margin_pct": "margin_rate",
        "maintenance_pct": "maintenance_rate", "cost_of_capital_pct": "cost_of_capital_rate",
    }

    tender = models.OneToOneField(Tender, on_delete=models.PROTECT, related_name="costing_rates", editable=False)
    shipping_pct = _rate_percent_field("Shipping, as % of import material (only when freight is not in the price).")
    duty_pct = _rate_percent_field("Duty, as % of CIF.")
    cnf_pct = _rate_percent_field("C&F, as % of CIF.")
    labour_rate_tzs_per_hour = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))],
        help_text="Labour rate in TZS per hour.",
    )
    overhead_pct = _rate_percent_field("Overhead.")
    margin_pct = _rate_percent_field("Gross operating margin.")
    maintenance_pct = _rate_percent_field("Maintenance and guarantee.")
    cost_of_capital_pct = _rate_percent_field(
        "Computed and stored, NOT added to the selling price (§4, open question)."
    )

    objects = TenderCostingRatesQuerySet.as_manager()

    class Meta:
        verbose_name = "tender costing rates"
        verbose_name_plural = "tender costing rates"
        constraints = [
            *[
                models.CheckConstraint(
                    condition=Q(**{f"{name}__gte": 0, f"{name}__lte": 100}),
                    name=f"tenders_costingrates_{name}_range",
                )
                for name in (
                    "shipping_pct", "duty_pct", "cnf_pct", "overhead_pct", "margin_pct",
                    "maintenance_pct", "cost_of_capital_pct",
                )
            ],
            models.CheckConstraint(
                condition=Q(labour_rate_tzs_per_hour__gte=0),
                name="tenders_costingrates_labour_rate_non_negative",
            ),
        ]

    def __str__(self):
        return f"Costing rates for {self.tender.reference}"

    # -- creation ---------------------------------------------------------------------

    @classmethod
    def baseline_values(cls):
        """The company defaults (CostingDefaults.current()) as this model's field values."""
        current = CostingDefaults.current()
        hundred, cents = Decimal("100"), Decimal("0.01")
        values = {pct: (getattr(current, frac) * hundred).quantize(cents) for pct, frac in cls.FRACTION_OF.items()}
        values["labour_rate_tzs_per_hour"] = current.labour_rate_tzs_per_hour
        return values

    @classmethod
    def create_for(cls, tender):
        """Copy the company baseline onto a tender (R22). Idempotent: an existing row is
        returned untouched."""
        existing = cls._base_manager.filter(tender_id=tender.pk).first()
        if existing is not None:
            return existing
        row = cls(tender=tender, **cls.baseline_values())
        try:
            with transaction.atomic(), row._system_write():
                row.save()
        except IntegrityError:  # created concurrently: one row per tender
            return cls._base_manager.get(tender_id=tender.pk)
        return row

    @classmethod
    def for_tender(cls, tender):
        """This tender's rates. Every tender gets its row at upload; one created any other
        way gets the company baseline the first time its rates are needed (audited)."""
        return cls.create_for(tender)

    # -- writes are the model's own (R22) -----------------------------------------------

    _write_allowed = False

    @contextmanager
    def _system_write(self):
        self._write_allowed = True
        try:
            yield
        finally:
            self._write_allowed = False

    @property
    def is_locked(self):
        """True unless the tender is DRAFTING or COSTING. Reads the stored status."""
        return _stored_status(self.tender_id) not in RATES_EDITABLE_STATUSES

    @staticmethod
    def _lock_error(status):
        return TransitionError(
            f"The tender's costing rates are locked: it is {status}. They can be changed only "
            f"while it is {' or '.join(RATES_EDITABLE_STATUSES)}; they lock when it leaves "
            "COSTING, and a rejection that returns it to COSTING (R9) unlocks them (R22).",
            code="r22",
        )

    def clean(self):
        super().clean()
        if not self._state.adding:
            status = _stored_status(self.tender_id)
            if status not in RATES_EDITABLE_STATUSES:
                raise self._lock_error(status)

    def save(self, *args, **kwargs):
        kwargs.pop("update_fields", None)
        if not self._write_allowed:
            raise GuardedFieldError(
                {"__all__": "A tender's costing rates are created with the tender and changed only "
                            "by TenderCostingRates.apply(), by the MD (R22)."}
            )
        if not self._state.adding:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("tender_id", flat=True).first()
            if stored is not None and stored != self.tender_id:
                raise GuardedFieldError({"tender": "Costing rates never move between tenders."})
            status = _stored_status(self.tender_id)
            if status not in RATES_EDITABLE_STATUSES:
                raise self._lock_error(status)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A tender's costing rates cannot be deleted (R22).")

    # -- reading ----------------------------------------------------------------------

    def values(self):
        return {name: getattr(self, name) for name in self.RATE_FIELDS}

    def rates(self):
        """These values as a CostingRates (fractions), as the costing chain takes them.
        VAT comes from CostingDefaults: it is bid-level and statutory, not a lever."""
        hundred = Decimal("100")
        fractions = {frac: Decimal(getattr(self, pct)) / hundred for pct, frac in self.FRACTION_OF.items()}
        return CostingRates(
            labour_rate_tzs_per_hour=Decimal(self.labour_rate_tzs_per_hour),
            vat_rate=CostingDefaults.current().vat_rate,
            **fractions,
        )

    def _candidate(self, overrides):
        """An unsaved copy with ``overrides`` applied and validated exactly like a real edit."""
        unknown = sorted(set(overrides) - set(self.RATE_FIELDS))
        if unknown:
            raise ValidationError({name: "Not a tender costing rate." for name in unknown})
        trial = type(self)(tender_id=self.tender_id, **{**self.values(), **overrides})
        trial.clean_fields(exclude=["tender"])  # ranges and decimal places; values converted
        return trial

    # -- preview and apply (R22) ------------------------------------------------------

    @staticmethod
    def _items(tender):
        items = tender._related("items")
        if items is None:  # fail closed, like every gate that needs an app below this one
            raise TransitionError("The items app is not installed: there is nothing to cost.", code="r22")
        return items

    def preview(self, **candidate):
        """The bid total if the rates were ``candidate`` (any subset of RATE_FIELDS; the rest
        as they are now). Saves NOTHING, so the MD can try values freely. Allowed when
        locked too: trying a number changes nothing."""
        rates = self._candidate(candidate).rates()
        total, priced, unpriced = Decimal(0), 0, 0
        for item in self._items(self.tender).all():  # reverse accessor: no import of items (§1)
            line = item.total_selling_at(rates)
            if line is None:
                unpriced += 1
            else:
                priced += 1
                total += line
        return BidPreview(total_selling=total, priced_items=priced, unpriced_items=unpriced)

    @staticmethod
    def _bid_total(items):
        return sum(
            (t for t in items.values_list("total_selling_price_tzs", flat=True) if t is not None), Decimal(0)
        )

    def apply(self, user, **new_values):
        """
        The MD sets this bid's rates (R22). In one transaction: refuse unless the actor is
        EXEC and the tender is DRAFTING or COSTING; save the new values (an UPDATE
        AuditEntry recording each old and new value, attributed to the MD); then re-cost
        every item at them (each item's own AuditEntry records its old and new figures).
        Values not given keep their current setting. Applying the current values is a no-op.
        """
        if user is None or not user.is_active or user.role != Role.EXECUTIVE:
            raise PermissionDenied("Only the MD (EXEC) may set a tender's costing rates (R22).")
        with transaction.atomic(), audit_actor(user):
            # Lock the tender row first, as the transition methods do: a concurrent
            # submit_for_audit waits, so the rates cannot change once the lock is taken.
            tender = Tender.objects.select_for_update().get(pk=self.tender_id)
            if tender.status not in RATES_EDITABLE_STATUSES:
                raise self._lock_error(tender.status)
            row = type(self)._base_manager.select_for_update().get(pk=self.pk)
            old_values = row.values()
            new_values = row._candidate(new_values).values()
            items = self._items(tender)
            old_total = self._bid_total(items)
            if new_values == old_values:
                return RatesApplied(old_values, new_values, old_total, old_total, 0)
            for name, value in new_values.items():
                setattr(row, name, value)
            row.full_clean(exclude=["tender"])
            with row._system_write():
                row.save()
            recosted = 0
            for item in items.select_for_update().order_by("pk"):
                item.recost_at_tender_rates()
                recosted += 1
            new_total = self._bid_total(items)
        self.refresh_from_db()
        return RatesApplied(old_values, new_values, old_total, new_total, recosted)
