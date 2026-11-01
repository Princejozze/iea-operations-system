"""
accounts — app 2 in CLAUDE.md §1. Imports only from core.

User identity rules (§2):
- One human = exactly one User. A person with two functions still gets one
  account; a second login would let a PM audit their own tender (R1).
- Sign-in is by email address. Additional historical addresses are stored as
  EmailAlias rows for legacy-import matching only; they cannot sign in.
- Email and every alias are unique across the whole system, case-insensitively.
"""

from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import Auditable, AuditableQuerySet


class Role(models.TextChoices):
    EXECUTIVE = "EXEC", "Executive (Managing Director)"
    HEAD_OF_OPERATIONS = "HOPS", "Head of Operations"
    PROJECT_MANAGER = "PM", "Project Manager"
    AUDITOR = "AUDIT", "Auditor"
    FINANCE = "FIN", "Finance"
    CLEARING_AGENT = "CLEAR", "Subcontractor / Clearing Agent"
    ADMIN = "ADMIN", "System Admin"


# §2 role table. Rank governs task assignment (R11) and nothing else; lower is
# senior. ADMIN is outside the hierarchy and has no rank.
ROLE_RANK = {
    Role.EXECUTIVE: 1,
    Role.HEAD_OF_OPERATIONS: 2,
    Role.PROJECT_MANAGER: 3,
    Role.AUDITOR: 3,
    Role.FINANCE: 3,
    Role.CLEARING_AGENT: 4,
    Role.ADMIN: None,
}

# R21: only these roles may create accounts.
PROVISIONING_ROLES = (Role.EXECUTIVE, Role.ADMIN)


def normalise_email(address):
    """Whole address lower-cased and trimmed, so uniqueness is case-insensitive."""
    return (address or "").strip().lower()


def _rank_matches_role_constraint():
    clauses = Q(role=Role.ADMIN, rank__isnull=True)
    for role, rank in ROLE_RANK.items():
        if rank is not None:
            clauses |= Q(role=role, rank=rank)
    return clauses


class UserManager(DjangoUserManager.from_queryset(AuditableQuerySet)):
    """
    Django's UserManager (create_user / create_superuser / natural-key lookup)
    combined with AuditableQuerySet, so queryset.update() and bulk_update() on
    users are refused like on every other audited model.

    Accounts for real staff are created through accounts.provisioning (R21),
    not by calling these directly; create_superuser exists for the one-off
    bootstrap `createsuperuser` command.
    """

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        email = normalise_email(email)
        if not email:
            raise ValueError("An email address is required.")
        if not extra_fields.get("role"):
            raise ValueError("A role is required (R21).")
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)

    def get_by_natural_key(self, username):
        return self.get(**{self.model.USERNAME_FIELD: normalise_email(username)})


class User(Auditable, AbstractUser):
    """IEA system user. One role per account, and one account per human (§2)."""

    username = None  # sign-in is by email

    email = models.EmailField("email address", unique=True)
    role = models.CharField(max_length=5, choices=Role.choices)
    rank = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        editable=False,
        help_text="Set from role. 1 is most senior. Empty for System Admin.",
    )

    # R21 provisioning state
    must_change_password = models.BooleanField(default=True)
    temporary_password_expires_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name", "role"]

    audit_redact = ("password",)

    objects = UserManager()

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"
        ordering = ["first_name", "last_name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=Role.values), name="accounts_user_role_valid"
            ),
            models.CheckConstraint(
                condition=_rank_matches_role_constraint(),
                name="accounts_user_rank_matches_role",
            ),
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique"),
        ]

    # -- invariants: checked in clean() and again in save() (§4 Enforcement) --

    def _apply_derived_fields(self):
        self.email = normalise_email(self.email)
        self.rank = ROLE_RANK.get(self.role)

    def _invariant_errors(self):
        errors = {}
        if self.role not in Role.values:
            errors["role"] = "A valid role is required (R21)."
        if not self.email:
            errors["email"] = "An email address is required."
        elif EmailAlias.objects.filter(address=self.email).exclude(user_id=self.pk).exists():
            errors["email"] = "This address is already an alias of another account."
        elif self.pk and self.email_aliases.filter(address=self.email).exists():
            errors["email"] = "This address is already one of this user's aliases."
        return errors

    def clean(self):
        super().clean()
        self._apply_derived_fields()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._apply_derived_fields()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)
        super().save(*args, **kwargs)

    # -- R21 ------------------------------------------------------------------

    @property
    def temporary_password_expired(self):
        return (
            self.must_change_password
            and self.temporary_password_expires_at is not None
            and self.temporary_password_expires_at <= timezone.now()
        )

    @property
    def can_provision_accounts(self):
        return self.is_active and self.role in PROVISIONING_ROLES

    # -- role helpers -----------------------------------------------------------

    @property
    def is_executive(self):
        return self.role == Role.EXECUTIVE

    @property
    def is_head_of_operations(self):
        return self.role == Role.HEAD_OF_OPERATIONS

    @property
    def is_project_manager(self):
        return self.role == Role.PROJECT_MANAGER

    @property
    def is_auditor(self):
        return self.role == Role.AUDITOR

    @property
    def is_finance(self):
        return self.role == Role.FINANCE

    @property
    def is_clearing_agent(self):
        return self.role == Role.CLEARING_AGENT

    @property
    def is_system_admin(self):
        return self.role == Role.ADMIN

    # -- R1 ---------------------------------------------------------------------

    def audit_refusal_reason(self, tender):
        """Why this user may not audit ``tender``, or None if they may (R1).

        Checked on the real foreign keys, not on role labels alone. The audits
        app's QCAudit.clean()/save() and Tender.record_audit_result() both call
        assert_can_audit(), so the rule lives in one place.
        """
        if not self.is_active:
            return "Inactive users cannot audit."
        if not self.is_auditor:
            return "Only a user with role Auditor may audit a tender."
        if tender.project_manager_id == self.pk:
            return "A PM cannot audit their own tender (R1)."
        return None

    def can_audit(self, tender):
        return self.audit_refusal_reason(tender) is None

    def assert_can_audit(self, tender):
        reason = self.audit_refusal_reason(tender)
        if reason:
            raise ValidationError(reason, code="r1")

    def __str__(self):
        name = self.get_full_name() or self.email
        return f"{name} ({self.get_role_display()})"


class EmailAlias(Auditable):
    """An additional historical address for a user — import matching only (§2)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="email_aliases")
    address = models.EmailField(unique=True)

    class Meta:
        verbose_name = "email alias"
        verbose_name_plural = "email aliases"
        ordering = ["address"]
        constraints = [
            models.UniqueConstraint(Lower("address"), name="accounts_alias_address_ci_unique"),
        ]

    def _invariant_errors(self):
        if not self.address:
            return {"address": "An address is required."}
        if User.objects.filter(email=self.address).exists():
            return {"address": "This address is already a user's primary email."}
        return {}

    def clean(self):
        super().clean()
        self.address = normalise_email(self.address)
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.address = normalise_email(self.address)
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.address
