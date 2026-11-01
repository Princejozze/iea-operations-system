"""
core — app 1 in CLAUDE.md §1. Everything else may import from here; this app
imports from no other local app. (The user model is referenced only through
``settings.AUTH_USER_MODEL``, the same way ``django.contrib.admin.LogEntry``
does it, so no Python import of ``accounts`` is needed.)

Contents:
- ``AuditEntry``  — the immutable audit trail (R7).
- ``TimeStamped`` — abstract: created_at / updated_at.
- ``Auditable``   — abstract: every model holding business data inherits this,
                    and every create / update / status change / delete on it
                    writes an ``AuditEntry`` automatically.
- ``Client``      — the client table (§3).
- ``TenderCategory`` — lookup table for Tender.category (§8).
- ``CostingDefaults`` — singleton company costing rates (§4); items snapshot them.
- ``ReferenceCounter`` — counter rows behind ``core.references`` (R20).
"""

import json
from contextlib import nullcontext
from dataclasses import dataclass, fields as dataclass_fields
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.db.models import Q
from django.db.models.fields.files import FieldFile
from django.db.models.functions import Lower
from django.utils import timezone

from .audit import audit_actor, get_current_actor


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuditTrailImmutableError(PermissionDenied):
    """Raised on any attempt to update or delete an AuditEntry (R7).

    Subclasses PermissionDenied so that, if one ever escapes into a view, the
    user sees a 403 rather than a 500.
    """


class AuditBypassError(Exception):
    """Raised by bulk operations that would change audited rows without
    writing audit entries (queryset.update(), bulk_update())."""


# ---------------------------------------------------------------------------
# AuditEntry — R7
# ---------------------------------------------------------------------------


class AuditAction(models.TextChoices):
    CREATE = "CREATE", "Create"
    UPDATE = "UPDATE", "Update"
    STATUS_CHANGE = "STATUS_CHANGE", "Status change"
    DELETE = "DELETE", "Delete"


class AuditEntryQuerySet(models.QuerySet):
    """Append-only: inserts are allowed, nothing else."""

    def update(self, **kwargs):
        raise AuditTrailImmutableError("Audit entries cannot be updated (R7).")

    def bulk_update(self, objs, fields, batch_size=None):
        raise AuditTrailImmutableError("Audit entries cannot be updated (R7).")

    def delete(self):
        raise AuditTrailImmutableError("Audit entries cannot be deleted (R7).")

    delete.queryset_only = True


class AuditEntry(models.Model):
    """
    One immutable record of one change to one object.

    Immutability is enforced in four layers, so no single bypass defeats it:
      1. ``save()`` refuses anything but the first insert.
      2. ``delete()`` and the queryset's ``update()`` / ``bulk_update()`` /
         ``delete()`` raise.
      3. The admin grants no add / change / delete permission, to anyone,
         including superusers; and the model defines no such permissions.
      4. On PostgreSQL a trigger rejects UPDATE and DELETE on the table, so
         even raw SQL from the application fails (migration 0002).
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        # PROTECT, not SET_NULL: SET_NULL would *update* audit rows when a user
        # is deleted. Users are deactivated, never deleted.
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Who made the change. Empty means the system (e.g. a cron job).",
    )
    action = models.CharField(max_length=16, choices=AuditAction.choices)
    model = models.CharField(
        max_length=100, help_text='Model label, e.g. "tenders.Tender".'
    )
    object_id = models.CharField(max_length=64)
    object_repr = models.CharField(
        max_length=255,
        blank=True,
        help_text="str() of the object at the time — survives the object's deletion.",
    )
    changes = models.JSONField(
        encoder=DjangoJSONEncoder,
        default=dict,
        blank=True,
        help_text='Field-level changes: {"field": [old, new], ...}.',
    )
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    objects = AuditEntryQuerySet.as_manager()

    class Meta:
        verbose_name = "audit entry"
        verbose_name_plural = "audit entries"
        ordering = ["-timestamp", "-id"]
        indexes = [models.Index(fields=["model", "object_id"])]
        # No add/change/delete permissions exist, so none can ever be granted.
        default_permissions = ("view",)
        # Related-object access and deletion collectors use the base manager;
        # make sure it is the append-only one too.
        base_manager_name = "objects"

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.action} {self.model}#{self.object_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditTrailImmutableError("Audit entries cannot be updated (R7).")
        kwargs["force_insert"] = True
        kwargs.pop("force_update", None)
        kwargs.pop("update_fields", None)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditTrailImmutableError("Audit entries cannot be deleted (R7).")


# ---------------------------------------------------------------------------
# Abstract bases
# ---------------------------------------------------------------------------


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def _json_safe(value):
    """Turn a field value into something JSONField stores and compares stably."""
    if isinstance(value, FieldFile):
        value = value.name or None
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


class AuditableQuerySet(models.QuerySet):
    """
    Blocks the bulk paths that would change rows without an audit entry.

    - ``update()`` / ``bulk_update()``: refused. Load the objects and save()
      them, or use the model's transition methods (R19).
    - ``delete()``: allowed — Django sends ``post_delete`` for every row,
      including cascades, and ``core.signals`` audits each one.
    - ``bulk_create()``: allowed, and writes one CREATE entry per row (the
      legacy importer needs this to stay fast).
    """

    def update(self, **kwargs):
        raise AuditBypassError(
            f"{self.model._meta.label}: queryset.update() bypasses the audit "
            "trail (R7). Save each instance, or use a transition method (R19)."
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise AuditBypassError(
            f"{self.model._meta.label}: bulk_update() bypasses the audit trail (R7)."
        )

    def bulk_create(self, objs, *args, **kwargs):
        if kwargs.get("ignore_conflicts") or kwargs.get("update_conflicts"):
            raise AuditBypassError(
                "bulk_create() with conflict handling cannot be audited reliably."
            )
        with transaction.atomic(using=self.db):
            created = super().bulk_create(objs, *args, **kwargs)
            actor = get_current_actor()
            AuditEntry.objects.using(self.db).bulk_create(
                [
                    AuditEntry(
                        actor=actor,
                        action=AuditAction.CREATE,
                        model=obj._meta.label,
                        object_id=str(obj.pk),
                        object_repr=str(obj)[:255],
                        changes=obj._audit_redact(
                            {k: [None, v] for k, v in obj._audit_snapshot().items()}
                        ),
                    )
                    for obj in created
                ]
            )
        return created


class Auditable(TimeStamped):
    """
    Inherit this for every model that holds business data (CLAUDE.md §1).

    Each save() writes one AuditEntry in the same transaction as the change:
      - CREATE        on first save, with every field as [None, value]
      - STATUS_CHANGE on a save that changes a field named ``status``
      - UPDATE        on any other save that changes something
      - (nothing)     on a save that changes nothing
    Each delete — direct, via queryset, or by cascade — writes a DELETE entry.

    Class attributes subclasses may override:
      audit_exclude  field names never recorded (default: updated_at)
      audit_redact   field names whose change is recorded but whose value is
                     replaced by "[redacted]" (e.g. ("password",) on User)
    """

    audit_exclude = ("updated_at",)
    audit_redact = ()

    objects = AuditableQuerySet.as_manager()

    class Meta:
        abstract = True

    # -- snapshots ----------------------------------------------------------

    def _audit_fields(self):
        return [
            f for f in self._meta.concrete_fields if f.name not in self.audit_exclude
        ]

    def _audit_snapshot(self):
        """Current in-memory values, keyed by attname (FKs as ``*_id``).
        Unredacted — pass through _audit_redact() before storing."""
        return {f.attname: _json_safe(getattr(self, f.attname)) for f in self._audit_fields()}

    def _audit_redact(self, changes):
        """Mask values of ``audit_redact`` fields. Called after diffing, so a
        changed password is still detected and recorded — just not its value."""
        redacted = {self._meta.get_field(name).attname for name in self.audit_redact}
        return {
            key: [None if v is None else "[redacted]" for v in pair] if key in redacted else pair
            for key, pair in changes.items()
        }

    def _audit_snapshot_from_db(self, using, fields=None):
        """Values as stored in the database, or None if the row is absent."""
        if fields is None:
            fields = self._audit_fields()
        row = (
            type(self)._base_manager.using(using)
            .filter(pk=self.pk)
            .values(*[f.attname for f in fields])
            .first()
        )
        if row is None:
            return None
        return {f.attname: _json_safe(row[f.attname]) for f in fields}

    # -- save / delete ------------------------------------------------------

    def save(self, *args, actor=None, **kwargs):
        using = kwargs.get("using") or self._state.db or "default"
        actor_ctx = audit_actor(actor) if actor is not None else nullcontext()
        with actor_ctx, transaction.atomic(using=using):
            fields = self._audit_fields()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                wanted = set(update_fields)
                fields = [f for f in fields if f.name in wanted or f.attname in wanted]

            before = None
            if not self._state.adding and self.pk is not None:
                before = self._audit_snapshot_from_db(using, fields)

            super().save(*args, **kwargs)

            # Read back from the database so the record shows what was
            # actually stored (normalised decimals, truncated times, etc.).
            after = self._audit_snapshot_from_db(using, fields) or {}

            if before is None:
                action = AuditAction.CREATE
                changes = {k: [None, v] for k, v in after.items()}
            else:
                changes = {
                    k: [before[k], after[k]] for k in after if before.get(k) != after[k]
                }
                if not changes:
                    return
                action = (
                    AuditAction.STATUS_CHANGE if "status" in changes else AuditAction.UPDATE
                )

            AuditEntry.objects.using(using).create(
                actor=get_current_actor(),
                action=action,
                model=self._meta.label,
                object_id=str(self.pk),
                object_repr=str(self)[:255],
                changes=self._audit_redact(changes),
            )

    save.alters_data = True

    def delete(self, *args, actor=None, **kwargs):
        # The DELETE entry itself is written by core.signals on post_delete,
        # which also catches queryset and cascade deletes.
        actor_ctx = audit_actor(actor) if actor is not None else nullcontext()
        with actor_ctx:
            return super().delete(*args, **kwargs)

    delete.alters_data = True


# ---------------------------------------------------------------------------
# Client — §3: "A real table in core, not free text."
# ---------------------------------------------------------------------------


class Client(Auditable):
    """The government body or institution issuing a tender."""

    name = models.CharField(
        max_length=255, help_text="Full name, e.g. 'Muhimbili National Hospital'."
    )
    short_name = models.CharField(
        max_length=50, blank=True, help_text="Common abbreviation, e.g. 'MNH'."
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            # Case-insensitive: "MSD" and "msd" are the same client. This is the
            # guard against the free-text duplication seen in the legacy data.
            models.UniqueConstraint(Lower("name"), name="core_client_name_ci_unique"),
        ]

    def __str__(self):
        return f"{self.name} ({self.short_name})" if self.short_name else self.name


# ---------------------------------------------------------------------------
# TenderCategory — §8 "Tender category": a lookup table, NOT an enum
# ---------------------------------------------------------------------------


class TenderCategory(Auditable):
    """
    What kind of equipment a tender is for. A table rather than an enum because
    the legacy data has a long tail of genuine one-offs (leather, textile, PPE,
    metallurgy): new categories must be addable in the admin without a
    migration. The initial rows are seeded by migration 0004.
    """

    name = models.CharField(max_length=100, help_text="Stored upper-case, e.g. 'CLINICAL LAB'.")

    class Meta:
        ordering = ["name"]
        verbose_name = "tender category"
        verbose_name_plural = "tender categories"
        constraints = [
            models.UniqueConstraint(Lower("name"), name="core_tendercategory_name_ci_unique"),
        ]

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split()).upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# CostingDefaults — §4 "Prices and the costing chain": a singleton, NOT settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostingRates:
    """
    An immutable copy of the company costing rates at one moment: what
    ``CostingDefaults.current()`` returns, and what an Item copies onto itself
    when it is costed (§4: changing a default must never reprice a past bid).

    All ``*_rate`` values are FRACTIONS (Decimal("0.10") is 10%), ready to
    multiply by. The database stores percentages for easier human editing; this
    is the only place the conversion happens.
    """

    shipping_rate: Decimal
    duty_rate: Decimal
    cnf_rate: Decimal
    labour_rate_tzs_per_hour: Decimal
    overhead_rate: Decimal
    margin_rate: Decimal
    maintenance_rate: Decimal
    vat_rate: Decimal
    cost_of_capital_rate: Decimal

    @property
    def labour_markup(self):
        """Multiplier from labour cost to labour selling price: 1 + overhead + margin.

        1.30 with the default rates, matching the template's literal x1.30
        ("overhead + margin on labour"), but it follows the rates if they are
        edited instead of being a hardcoded 1.30.
        """
        return 1 + self.overhead_rate + self.margin_rate

    def as_dict(self):
        """Plain dict of field name -> Decimal, for copying onto an Item."""
        return {f.name: getattr(self, f.name) for f in dataclass_fields(self)}


def _percent_field(default, help_text):
    return models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal(default),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=f"Percent, e.g. 10 means 10%. {help_text}",
    )


class CostingDefaultsQuerySet(AuditableQuerySet):
    def delete(self):
        raise ValidationError("The costing defaults are a singleton and cannot be deleted.")

    delete.queryset_only = True


class CostingDefaults(Auditable):
    """
    The company's costing rates, from IMPERIAL EAST AFRICA LIMITED MASTER
    TEMPLATE 2026. Exactly one row, always pk=1.

    Why a table and not settings.py (§4): the MD can change a margin without a
    deploy; every change is audited (R7), so an old bid can be explained with the
    rates that applied then; and ``items`` never touches a shared file.

    Items must SNAPSHOT these per costed line: call ``CostingDefaults.current()``
    and store the values, so editing a default never silently reprices a past
    bid. Never read this table when re-displaying an existing item.
    """

    SINGLETON_PK = 1
    PERCENT_FIELDS = (
        "shipping_pct", "duty_pct", "cnf_pct", "overhead_pct", "margin_pct",
        "maintenance_pct", "vat_pct", "cost_of_capital_pct",
    )

    shipping_pct = _percent_field("10", "Shipping, as % of FOB material cost (import material).")
    duty_pct = _percent_field("25", "Duty, as % of CIF cost.")
    cnf_pct = _percent_field("5", "C&F, as % of CIF cost.")
    labour_rate_tzs_per_hour = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("15000"),
        validators=[MinValueValidator(Decimal("0"))], help_text="Labour rate in TZS per hour.",
    )
    overhead_pct = _percent_field("10", "Overhead.")
    margin_pct = _percent_field("20", "Gross operating margin.")
    maintenance_pct = _percent_field("1", "Maintenance and guarantee.")
    vat_pct = _percent_field("18", "VAT, applied at bid-summary level, not per item.")
    cost_of_capital_pct = _percent_field(
        "22.25",
        "UNRESOLVED (CLAUDE.md section 4): the template multiplies total material by 22.25% "
        "but labels the column 'annual interest rate 20%, t=3 months'. Calculated and "
        "stored, but NOT added to the selling price until Faraja answers.",
    )

    objects = CostingDefaultsQuerySet.as_manager()

    class Meta:
        verbose_name = "costing defaults"
        verbose_name_plural = "costing defaults"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="core_costingdefaults_singleton"),
            *[
                models.CheckConstraint(
                    condition=Q(**{f"{name}__gte": 0, f"{name}__lte": 100}),
                    name=f"core_costingdefaults_{name}_range",
                )
                for name in (
                    "shipping_pct", "duty_pct", "cnf_pct", "overhead_pct", "margin_pct",
                    "maintenance_pct", "vat_pct", "cost_of_capital_pct",
                )
            ],
            models.CheckConstraint(
                condition=Q(labour_rate_tzs_per_hour__gte=0),
                name="core_costingdefaults_labour_rate_non_negative",
            ),
        ]

    def __str__(self):
        return "Costing defaults"

    def save(self, *args, **kwargs):
        if self.pk not in (None, self.SINGLETON_PK):
            raise ValidationError("The costing defaults are a singleton (pk=1).")
        if self._state.adding and type(self)._base_manager.filter(pk=self.SINGLETON_PK).exists():
            raise ValidationError("The costing defaults already exist; edit them instead.")
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("The costing defaults are a singleton and cannot be deleted.")

    def rates(self):
        """This row's values as an immutable CostingRates."""
        hundred = Decimal("100")
        return CostingRates(
            shipping_rate=self.shipping_pct / hundred,
            duty_rate=self.duty_pct / hundred,
            cnf_rate=self.cnf_pct / hundred,
            labour_rate_tzs_per_hour=self.labour_rate_tzs_per_hour,
            overhead_rate=self.overhead_pct / hundred,
            margin_rate=self.margin_pct / hundred,
            maintenance_rate=self.maintenance_pct / hundred,
            vat_rate=self.vat_pct / hundred,
            cost_of_capital_rate=self.cost_of_capital_pct / hundred,
        )

    @classmethod
    def current(cls):
        """
        The current company rates as an immutable CostingRates snapshot. This is
        the method ``items`` calls when costing a line.

        Reads the database every time (never cached), so an edit applies to the
        next costing but not to values already copied onto earlier items. If the
        row is somehow missing it is recreated with the section 4 values and
        audited, rather than failing in the middle of costing a bid.
        """
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj.rates()


# ---------------------------------------------------------------------------
# ReferenceCounter — R20, used only by core.references
# ---------------------------------------------------------------------------


class ReferenceCounter(models.Model):
    """
    One row per numbering sequence, e.g. key "tender:2026" or
    "item:IEA-2026-0042". Locked with select_for_update() while a number is
    taken, so two transactions can never receive the same value.

    Deliberately not Auditable: it is plumbing, and the record that receives
    the number (the Tender or Item) is audited — its CREATE entry carries the
    reference.
    """

    key = models.CharField(max_length=64, unique=True)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.key} = {self.last_value}"
