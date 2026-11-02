"""
items — app 6 in CLAUDE.md §1.

Holds: Item, ItemAttachment, Quote, SupplierSuggestion, ExchangeRate, RFQ, the
costing chain (``items.costing``, §4), currency conversion, quote entry and the
xlsx round-trip (``items.services``), RFQ dispatch (``items.services.rfq``).
Bulk specification import, AI discovery and supplier chasing are later work.

May import from: core, accounts, documents, suppliers, tenders. Never from apps below it.
Every model holding business data inherits core.models.Auditable (R7).

Contract with tenders (read by Tender's transition methods through reverse
accessors — see tenders/models.py docstring):
    Item.tender               ForeignKey(Tender, related_name="items")
    ItemAttachment.item       ForeignKey(Item, related_name="attachments"),
                              field ``kind`` in BROCHURE | QUOTATION | DATASHEET
    Item fields checked by R2: unit_cost, supplier, currency, incoterm,
    unit_of_measure, category, make_brand, model_part_number, country_of_origin

Rules enforced here
  R6   every Item has a tender (mandatory FK); orphans are impossible.
  R10  money is computed, never entered: every figure is recomputed in
       ``Item.save()`` and the database refuses a total that contradicts its
       parts. ``queryset.update()`` and ``bulk_create()`` are refused.
  R15  no item, costing or RFQ work before the MD registers the tender: every
       write to Item / ItemAttachment / Quote / SupplierSuggestion calls
       ``assert_item_work_allowed``.
  R9a  ...and none while the tender is frozen awaiting an eligibility decision:
       the same call starts with ``Tender.require_unfrozen()``.
  R4   rates float while the tender is in COSTING; once the tender holds a rate
       lock no item can change (it is no longer DRAFTING/COSTING).
  R8   every RFQ row and every RFQ email carries sales@ in cc (documents.rfq).

Quotes (§5 step 13, §7). A quote is the supplier's original document AND the
typed figures; neither substitutes for the other. ``Quote.original_document`` is
an ItemAttachment of kind QUOTATION on the same item, so the evidence also
counts toward R2's three attachments. ``Quote.select()`` is the only writer of
``Quote.status`` and ``Item.selected_quote``: it copies the quote's figures onto
the item (QUOTE_TO_ITEM), the item re-costs, and the change is audited. While a
quote is selected, Item.save() refuses item figures that disagree with it — a
bid price must always be traceable to the document behind it.
"""

from contextlib import contextmanager
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q
from django.db.models.signals import m2m_changed, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from accounts.models import Role
from core.audit import audit_actor
from core.models import AuditBypassError, Auditable, AuditableQuerySet, CostingRates
from core.references import next_item_reference
from documents.models import Document
from documents.rfq import SALES_CC, normalise_recipients, with_required_cc
from suppliers.models import Supplier
from tenders.models import (
    WORK_ALLOWED_STATUSES,
    Currency,
    GuardedFieldError,
    Tender,
    TenderCostingRates,
    TransitionError,
)

from .costing import CostingInputs, compute_costing, freight_included

# ---------------------------------------------------------------------------
# Vocabularies (§7, §8)
# ---------------------------------------------------------------------------


class ItemStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    QUOTED_COMPLIED = "QUOTED_COMPLIED", "Quoted, complied"
    NEEDS_RESOLUTION = "NEEDS_RESOLUTION", "Needs resolution"
    CLARIFICATION_PENDING = "CLARIFICATION_PENDING", "Clarification pending"


class Incoterm(models.TextChoices):
    CIF = "CIF", "CIF"
    EXW = "EXW", "EXW"
    FOB = "FOB", "FOB"
    CFR = "CFR", "CFR"
    FCA = "FCA", "FCA"


class SourcingType(models.TextChoices):
    COMPLETE_UNIT = "COMPLETE_UNIT", "Complete unit"
    SEPARATE_PART = "SEPARATE_PART", "Separate part"


class AttachmentKind(models.TextChoices):
    BROCHURE = "BROCHURE", "Brochure"
    QUOTATION = "QUOTATION", "Quotation"
    DATASHEET = "DATASHEET", "Datasheet"


class QuoteStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    SELECTED = "SELECTED", "Selected"
    REJECTED = "REJECTED", "Rejected"


class SuggestionStatus(models.TextChoices):
    SUGGESTED = "SUGGESTED", "Suggested"
    ACCEPTED = "ACCEPTED", "Accepted"
    REJECTED = "REJECTED", "Rejected"


# ---------------------------------------------------------------------------
# Guards shared by every model in this app
# ---------------------------------------------------------------------------


def _stored_tender_status(tender):
    return Tender._base_manager.filter(pk=tender.pk).values_list("status", flat=True).first()


def assert_item_work_allowed(tender, action):
    """R9a then R15: refuse item, costing, quote and sourcing work unless the
    tender is registered and not frozen. Reads the stored status, never a stale copy."""
    tender.require_unfrozen(action)  # R9a
    status = _stored_tender_status(tender)
    if status not in WORK_ALLOWED_STATUSES:
        raise TransitionError(
            f"{action} is refused: the tender is {status}; item work is allowed only "
            f"while it is {' or '.join(WORK_ALLOWED_STATUSES)} (R15, R4).",
            code="r15",
        )


def _is_tenders_pm(user, tender):
    return (
        user is not None and user.is_active and user.role == Role.PROJECT_MANAGER
        and Tender._base_manager.filter(pk=tender.pk, project_manager_id=user.pk).exists()
    )


class GuardedWrites:
    """Fields only a method of the model itself may write (§4: status is never set
    by assignment, admin or ``queryset.update()``). Outside ``with self._system_write():``
    a new row must carry the field defaults and an existing row must not change them."""

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


def _decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _money(**kwargs):
    return models.DecimalField(max_digits=24, decimal_places=6, null=True, blank=True, editable=False, **kwargs)


# ---------------------------------------------------------------------------
# ExchangeRate — currency conversion (§1), R4
# ---------------------------------------------------------------------------


class ExchangeRate(Auditable):
    """TZS per one unit of ``currency`` from ``as_of`` onwards. The rate an item
    uses is the latest one at or before the moment of costing — or, once the tender
    holds a rate lock (R4), at or before the lock. TZS itself is always 1."""

    currency = models.CharField(max_length=3, choices=Currency.choices)
    rate_to_tzs = models.DecimalField(max_digits=18, decimal_places=6, help_text="TZS per 1 unit of the currency.")
    as_of = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=100, blank=True, help_text="e.g. 'manual' or the rate provider.")

    class Meta:
        ordering = ["-as_of", "-id"]
        indexes = [models.Index(fields=["currency", "-as_of"])]
        constraints = [
            models.CheckConstraint(condition=~Q(currency=Currency.TZS), name="items_exchangerate_not_tzs"),
            models.CheckConstraint(condition=Q(rate_to_tzs__gt=0), name="items_exchangerate_positive"),
            models.CheckConstraint(condition=Q(currency__in=Currency.values), name="items_exchangerate_currency_valid"),
        ]

    def __str__(self):
        return f"1 {self.currency} = {self.rate_to_tzs} TZS (from {self.as_of:%Y-%m-%d})"

    @classmethod
    def rate_for(cls, currency, at):
        """The rate in force for ``currency`` at ``at``, or None if there is none yet."""
        return cls.objects.filter(currency=currency, as_of__lte=at).order_by("-as_of", "-id").first()


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class ItemQuerySet(AuditableQuerySet):
    def bulk_create(self, objs, *args, **kwargs):
        raise AuditBypassError(
            "Item.objects.bulk_create() would skip the reference, costing and R15/R9a "
            "checks in Item.save(). Save each item (100 rows is fine)."
        )


class Item(GuardedWrites, Auditable):
    # -- required to save (§7) ---------------------------------------------------
    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="items")  # R6
    specification = models.TextField()
    quantity = models.PositiveIntegerField()

    # -- system-set (§7) ---------------------------------------------------------
    item_reference = models.CharField(max_length=24, unique=True, editable=False)
    status = models.CharField(
        max_length=24, choices=ItemStatus.choices, default=ItemStatus.DRAFT, editable=False
    )

    # -- required before QC audit (R2) ---------------------------------------------
    unit_cost = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
        help_text="What the supplier charges, in `currency`, on `incoterm` (what a quote gives you).",
    )
    currency = models.CharField(max_length=3, choices=Currency.choices, blank=True)
    incoterm = models.CharField(max_length=3, choices=Incoterm.choices, default=Incoterm.CIF)
    unit_of_measure = models.CharField(max_length=50, blank=True)
    # Free text, not the Tender category lookup: R2 tests it for null or "".
    category = models.CharField(max_length=100, blank=True)
    make_brand = models.CharField(max_length=255, blank=True)
    model_part_number = models.CharField(max_length=255, blank=True)
    country_of_origin = models.CharField(max_length=100, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    sourcing_type = models.CharField(max_length=16, choices=SourcingType.choices, blank=True)
    selected_quote = models.ForeignKey(
        "Quote", on_delete=models.PROTECT, null=True, blank=True, related_name="+", editable=False,
        help_text="The quote this item is costed on. Set only by Quote.select().",
    )
    # `lot` (§7: "if set, must belong to the same Tender") arrives with tenders.Lot.

    # -- costing inputs (§4) -------------------------------------------------------
    fob_unit_price = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
        help_text="FOB unit price in `currency`: the chain's import input (§4).",
    )
    local_unit_cost = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
        help_text="Locally sourced material cost per unit, in TZS.",
    )
    shipping_applies = models.BooleanField(default=True, help_text="Imported (True) vs locally sourced (False).")
    risk_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0"), help_text="Risk management, percent (5 means 5%)."
    )
    labour_hours_per_unit = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))

    # -- computed, never entered (R10) -----------------------------------------------
    total_cost = models.DecimalField(max_digits=24, decimal_places=4, null=True, blank=True, editable=False)
    unit_selling_price_tzs = models.BigIntegerField(null=True, blank=True, editable=False)  # chain: unit_rate
    total_selling_price_tzs = _money()  # chain: total_selling

    # The rate used and when it was in force (R4); null for a TZS-only or unpriced line.
    fx_rate = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True, editable=False)
    fx_rate_as_of = models.DateTimeField(null=True, blank=True, editable=False)

    # Every intermediate figure of the chain, in TZS (§4: "store every intermediate value").
    import_material = _money()
    shipping = _money()
    cif = _money()
    duty = _money()
    cnf = _money()
    local_material = _money()
    total_material = _money()
    cost_of_capital = _money(help_text="Computed and stored; NOT part of total_selling (§4, open question).")
    risk_management = _money()
    overhead = _money()
    margin = _money()
    material_selling = _money()
    labour_hours = _money()
    labour_cost = _money()
    labour_selling = _money()
    maintenance = _money()

    # The rates this item was costed with: its tender's TenderCostingRates (R22), copied
    # at first costing): changing a default never silently reprices a past bid (§4).
    # Fractions, not percents. Refreshed only by recost_at_tender_rates(), which the
    # MD's TenderCostingRates.apply() runs on every item of the tender.
    rates_snapshot_at = models.DateTimeField(null=True, blank=True, editable=False)
    shipping_rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)
    duty_rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)
    cnf_rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)
    labour_rate_tzs_per_hour = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, editable=False)
    overhead_rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)
    margin_rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)
    maintenance_rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)
    vat_rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)  # bid level, kept for the record
    cost_of_capital_rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, editable=False)

    objects = ItemQuerySet.as_manager()
    GUARDED_FIELDS = ("status", "item_reference", "selected_quote")

    # Quote field -> Item field, copied by Quote.select() (§5 step 13). The quoted
    # price is both what the supplier charges (unit_cost) and the chain's import
    # input (fob_unit_price holds the price as quoted, on `incoterm`, whatever the term:
    # CIF/CFR add no shipping, EXW/FOB/FCA do; with shipping_applies False the chain
    # adds no shipping, duty or C&F, so a local quote costs correctly too).
    # local_unit_cost, risk_pct and labour hours stay the PM's own inputs.
    QUOTE_TO_ITEM = (
        ("supplier_id", "supplier_id"),
        ("quoted_unit_price", "unit_cost"),
        ("quoted_unit_price", "fob_unit_price"),
        ("currency", "currency"),
        ("incoterm", "incoterm"),
        ("shipping_applies", "shipping_applies"),
        ("country_of_origin", "country_of_origin"),
        ("make_brand", "make_brand"),
        ("model_part_number", "model_part_number"),
    )

    RATE_FIELDS = (
        "shipping_rate", "duty_rate", "cnf_rate", "labour_rate_tzs_per_hour", "overhead_rate",
        "margin_rate", "maintenance_rate", "vat_rate", "cost_of_capital_rate",
    )
    # chain figure -> Item field (all but two share a name)
    BREAKDOWN_FIELDS = {
        "import_material": "import_material", "shipping": "shipping", "cif": "cif", "duty": "duty",
        "cnf": "cnf", "local_material": "local_material", "total_material": "total_material",
        "cost_of_capital": "cost_of_capital", "risk_management": "risk_management",
        "overhead": "overhead", "margin": "margin", "material_selling": "material_selling",
        "labour_hours": "labour_hours", "labour_cost": "labour_cost", "labour_selling": "labour_selling",
        "maintenance": "maintenance", "total_selling": "total_selling_price_tzs",
        "unit_rate": "unit_selling_price_tzs",
    }

    class Meta:
        ordering = ["tender_id", "item_reference"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="items_item_quantity_positive"),
            models.CheckConstraint(condition=Q(status__in=ItemStatus.values), name="items_item_status_valid"),
            models.CheckConstraint(condition=Q(currency__in=["", *Currency.values]), name="items_item_currency_valid"),
            models.CheckConstraint(condition=Q(incoterm__in=Incoterm.values), name="items_item_incoterm_valid"),
            models.CheckConstraint(
                condition=Q(sourcing_type__in=["", *SourcingType.values]), name="items_item_sourcing_type_valid"
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__isnull=True) | Q(unit_cost__gte=0), name="items_item_unit_cost_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(fob_unit_price__isnull=True) | Q(fob_unit_price__gte=0), name="items_item_fob_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(local_unit_cost__isnull=True) | Q(local_unit_cost__gte=0), name="items_item_local_non_negative"
            ),
            models.CheckConstraint(condition=Q(risk_pct__gte=0, risk_pct__lte=100), name="items_item_risk_pct_range"),
            models.CheckConstraint(condition=Q(labour_hours_per_unit__gte=0), name="items_item_labour_hours_non_negative"),
            # R10: total_cost = unit_cost x quantity, always — and absent exactly when unit_cost is.
            models.CheckConstraint(
                condition=(
                    Q(unit_cost__isnull=True, total_cost__isnull=True)
                    | Q(
                        unit_cost__isnull=False,
                        total_cost=ExpressionWrapper(
                            F("unit_cost") * F("quantity"), output_field=DecimalField(max_digits=30, decimal_places=4)
                        ),
                    )
                ),
                name="items_item_total_cost_is_unit_cost_times_quantity",
            ),
            # R10: the unit rate is floor(total_selling / quantity), and both exist or neither does.
            models.CheckConstraint(
                condition=(
                    Q(unit_selling_price_tzs__isnull=True, total_selling_price_tzs__isnull=True)
                    | Q(
                        unit_selling_price_tzs__isnull=False,
                        total_selling_price_tzs__isnull=False,
                        total_selling_price_tzs__gte=ExpressionWrapper(
                            F("unit_selling_price_tzs") * F("quantity"),
                            output_field=DecimalField(max_digits=40, decimal_places=6),
                        ),
                        total_selling_price_tzs__lt=ExpressionWrapper(
                            (F("unit_selling_price_tzs") + 1) * F("quantity"),
                            output_field=DecimalField(max_digits=40, decimal_places=6),
                        ),
                    )
                ),
                name="items_item_unit_rate_is_floor_of_total_over_quantity",
            ),
            # Nothing priced without the rates it was priced with.
            models.CheckConstraint(
                condition=Q(total_selling_price_tzs__isnull=True) | Q(rates_snapshot_at__isnull=False),
                name="items_item_priced_has_rates_snapshot",
            ),
        ]

    def __str__(self):
        return f"{self.item_reference or '(new item)'} — {self.specification[:60]}"

    # -- validation -----------------------------------------------------------------

    def _invariant_errors(self):
        errors = {}
        if not (self.specification or "").strip():
            errors["specification"] = "Describe the item."
        if self.quantity is None or self.quantity < 1:
            errors["quantity"] = "The quantity must be at least 1."
        for name in ("unit_cost", "fob_unit_price", "local_unit_cost"):
            value = getattr(self, name)
            if value is not None and _decimal(value) < 0:
                errors[name] = "Cannot be negative."
        if not (Decimal(0) <= _decimal(self.risk_pct) <= 100):
            errors["risk_pct"] = "A percentage between 0 and 100."
        if _decimal(self.labour_hours_per_unit) < 0:
            errors["labour_hours_per_unit"] = "Cannot be negative."
        if self.currency not in ("", *Currency.values):
            errors["currency"] = "Unknown currency."
        if self.incoterm not in Incoterm.values:
            errors["incoterm"] = "Unknown Incoterm."
        if self.sourcing_type not in ("", *SourcingType.values):
            errors["sourcing_type"] = "Unknown sourcing type."
        if self.fob_unit_price is not None and not self.currency:
            errors["currency"] = "A FOB price needs its currency."
        return errors

    def clean(self):
        super().clean()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    # -- costing (§4, R10, R4) -------------------------------------------------------

    @property
    def costing_rates(self):
        """The snapshotted rates as a CostingRates, or None if never costed."""
        if self.rates_snapshot_at is None:
            return None
        return CostingRates(**{name: _decimal(getattr(self, name)) for name in self.RATE_FIELDS})

    @property
    def is_costed(self):
        return self.total_selling_price_tzs is not None

    @property
    def specification_offered(self):
        """What the supplier committed to, from the selected quote ("" if none).
        audits.QCAuditLine.specification_offered is prefilled from this (§5 step 13)."""
        if self.selected_quote_id is None:
            return ""
        return Quote._base_manager.filter(pk=self.selected_quote_id).values_list(
            "specification_offered", flat=True
        ).first() or ""

    def _selected_quote_errors(self):
        """While a quote is selected, the copied figures must be the quote's."""
        if self.selected_quote_id is None:
            return {}
        quote = Quote._base_manager.filter(pk=self.selected_quote_id).first()
        if quote is None or quote.item_id != self.pk:
            return {"selected_quote": "The selected quote must be a quote for this item."}
        errors = {}
        for quote_field, item_field in self.QUOTE_TO_ITEM:
            mine, theirs = getattr(self, item_field), getattr(quote, quote_field)
            if isinstance(theirs, Decimal):
                mine = None if mine is None else _decimal(mine)
            if mine != theirs:
                name = item_field.removesuffix("_id")
                errors[name] = (
                    f"This item is costed on the selected quote ({theirs!s} there). "
                    "Enter and select a new quote to change it."
                )
        return errors

    def _clear_costing(self):
        for field in (*self.BREAKDOWN_FIELDS.values(), *self.RATE_FIELDS, "fx_rate", "fx_rate_as_of", "rates_snapshot_at"):
            setattr(self, field, None)

    def _exchange_rate(self):
        """(rate, as_of) for this item's currency, as of the tender's rate lock if it
        holds one (R4), otherwise now."""
        if self.fob_unit_price is None:
            return None, None
        if self.currency == Currency.TZS:
            return Decimal(1), None
        locked_at = (
            Tender._base_manager.filter(pk=self.tender_id).values_list("exchange_rate_locked_at", flat=True).first()
        )
        found = ExchangeRate.rate_for(self.currency, locked_at or timezone.now())
        if found is None:
            raise ValidationError({"currency": f"No exchange rate for {self.currency} is on record (add one before costing)."})
        return found.rate_to_tzs, found.as_of

    def _apply_costing(self):
        """Recompute every derived figure. Nothing here is ever taken from the caller (R10)."""
        self.total_cost = None if self.unit_cost is None else _decimal(self.unit_cost) * self.quantity
        if self.fob_unit_price is None and self.local_unit_cost is None:
            self._clear_costing()  # nothing to cost yet: unpriced
            return
        if self.rates_snapshot_at is None:  # first costing: copy the tender's rates onto the item (R22)
            for name, value in TenderCostingRates.for_tender(self.tender).rates().as_dict().items():
                setattr(self, name, value)
            self.rates_snapshot_at = timezone.now()
        fx_rate, fx_as_of = self._exchange_rate()
        self.fx_rate, self.fx_rate_as_of = fx_rate, fx_as_of
        result = compute_costing(self._costing_inputs(fx_rate), self.costing_rates)
        for figure, field in self.BREAKDOWN_FIELDS.items():
            setattr(self, field, getattr(result, figure))

    def _costing_inputs(self, fx_rate):
        return CostingInputs(
            fob_unit_price=_decimal(self.fob_unit_price or 0),
            fx_rate=fx_rate if fx_rate is not None else Decimal(1),
            local_unit_cost=_decimal(self.local_unit_cost or 0),
            quantity=self.quantity,
            shipping_applies=self.shipping_applies,
            risk_rate=_decimal(self.risk_pct) / 100,
            labour_hours_per_unit=_decimal(self.labour_hours_per_unit),
            freight_included=freight_included(self.incoterm),
        )

    def total_selling_at(self, rates):
        """This line's total_selling (TZS, ex VAT) at ``rates``, a CostingRates, WITHOUT
        saving anything: the MD's what-if (TenderCostingRates.preview, R22). Same chain,
        same exchange rate as a real costing. None if the item has no price yet."""
        if self.fob_unit_price is None and self.local_unit_cost is None:
            return None
        fx_rate, _ = self._exchange_rate()
        return compute_costing(self._costing_inputs(fx_rate), rates).total_selling

    def recost_at_tender_rates(self):
        """Re-copy the tender's current rates (TenderCostingRates, R22) onto this item and
        reprice it. Run on every item by TenderCostingRates.apply(); the only way an
        existing item's rates change, so editing CostingDefaults never reprices a bid (§4)."""
        self.rates_snapshot_at = None
        self.save()

    # -- save / delete ----------------------------------------------------------------

    def save(self, *args, **kwargs):
        kwargs.pop("update_fields", None)  # the derived figures must always be written together
        self._check_guard()
        with transaction.atomic():
            adding = self._state.adding
            assert_item_work_allowed(self.tender, "Saving an item")
            if not adding:
                stored = type(self)._base_manager.filter(pk=self.pk).values("tender_id", "item_reference").first()
                if stored and (stored["tender_id"] != self.tender_id or stored["item_reference"] != self.item_reference):
                    raise GuardedFieldError({"tender": "An item never moves between tenders or changes reference."})
            errors = self._invariant_errors()
            if not adding:
                errors.update(self._selected_quote_errors())
            elif self.selected_quote_id is not None:
                errors["selected_quote"] = "A new item has no quotes to select yet."
            if errors:
                raise ValidationError(errors)
            if adding:
                self.item_reference = next_item_reference(self.tender.reference)  # R20, same transaction
            self._apply_costing()
            super().save(*args, **kwargs)

    def set_status(self, user, status):
        """The item's workflow status. Written only here (§8): the tender's PM, while work is allowed."""
        if status not in ItemStatus.values:
            raise ValidationError({"status": f"Unknown item status {status!r}."})
        if not _is_tenders_pm(user, self.tender):
            raise PermissionDenied("Only this tender's PM can set an item's status.")
        with transaction.atomic(), audit_actor(user):
            self.refresh_from_db()
            self.status = status
            with self._system_write():
                self.save()


@receiver(pre_delete, sender=Item, dispatch_uid="items.guard_item_delete")
def _guard_item_delete(sender, instance, **kwargs):
    assert_item_work_allowed(instance.tender, "Deleting an item")


# ---------------------------------------------------------------------------
# ItemAttachment
# ---------------------------------------------------------------------------


class ItemAttachment(Auditable):
    """A brochure, quotation or datasheet for an item. R2 needs all three kinds
    before audit. Deleting one writes an AuditEntry (R7, via core.signals)."""

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="attachments")
    kind = models.CharField(max_length=16, choices=AttachmentKind.choices)
    file = models.FileField(upload_to="item_attachments/%Y/%m/")
    original_filename = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["item_id", "kind", "created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=AttachmentKind.values), name="items_itemattachment_kind_valid"),
        ]

    def __str__(self):
        return f"{self.item.item_reference}: {self.get_kind_display()} ({self.original_filename or self.file.name})"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            assert_item_work_allowed(self.item.tender, "Saving an attachment")
            if self.kind not in AttachmentKind.values:
                raise ValidationError({"kind": "Unknown attachment kind."})
            super().save(*args, **kwargs)


@receiver(pre_delete, sender=ItemAttachment, dispatch_uid="items.guard_attachment_delete")
def _guard_attachment_delete(sender, instance, **kwargs):
    assert_item_work_allowed(instance.item.tender, "Deleting an attachment")


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------


class QuoteQuerySet(AuditableQuerySet):
    def bulk_create(self, objs, *args, **kwargs):
        raise AuditBypassError(
            "Quote.objects.bulk_create() would skip the evidence, PM and R15/R9a checks in "
            "Quote.save(). Use items.services.quotes.enter_quote()."
        )


class Quote(GuardedWrites, Auditable):
    """
    What a supplier actually offered for an item (§5 step 13, §7).

    Both halves are mandatory: ``original_document`` is the supplier's quotation
    exactly as sent (the evidence), and the typed figures are what the costing
    chain runs on. Enter one through ``items.services.quotes.enter_quote`` (or
    the xlsx import), which stores the document and the figures together.

    The figures can be corrected while the quote is RECEIVED. Once SELECTED or
    REJECTED they are a record: enter a new quote instead.
    """

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="quotes")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="+")
    original_document = models.OneToOneField(
        ItemAttachment, on_delete=models.PROTECT, related_name="quote",
        help_text="The supplier's quotation as sent: a QUOTATION attachment of the same item.",
    )
    quoted_unit_price = models.DecimalField(max_digits=18, decimal_places=4)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    incoterm = models.CharField(max_length=3, choices=Incoterm.choices, default=Incoterm.CIF)
    shipping_applies = models.BooleanField(default=True, help_text="Imported (True) vs locally sourced (False).")
    country_of_origin = models.CharField(max_length=100, blank=True)
    make_brand = models.CharField(max_length=255, blank=True)
    model_part_number = models.CharField(max_length=255, blank=True)
    specification_offered = models.TextField(
        blank=True, help_text="What the supplier commits to. Flows into the QC audit line."
    )
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(
        max_length=16, choices=QuoteStatus.choices, default=QuoteStatus.RECEIVED, editable=False
    )

    objects = QuoteQuerySet.as_manager()
    GUARDED_FIELDS = ("status",)
    # Fixed once the quote is SELECTED or REJECTED (notes stay editable).
    FIGURE_FIELDS = (
        "item_id", "supplier_id", "original_document_id", "quoted_unit_price", "currency", "incoterm",
        "shipping_applies", "country_of_origin", "make_brand", "model_part_number",
        "specification_offered", "lead_time_days", "valid_until", "received_at", "entered_by_id",
    )

    class Meta:
        ordering = ["item_id", "quoted_unit_price", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(quoted_unit_price__gte=0), name="items_quote_unit_price_non_negative"),
            models.CheckConstraint(condition=Q(currency__in=Currency.values), name="items_quote_currency_valid"),
            models.CheckConstraint(condition=Q(incoterm__in=Incoterm.values), name="items_quote_incoterm_valid"),
            models.CheckConstraint(condition=Q(status__in=QuoteStatus.values), name="items_quote_status_valid"),
            # §7: an item may hold several quotes; exactly one may be SELECTED.
            models.UniqueConstraint(
                fields=["item"], condition=Q(status=QuoteStatus.SELECTED), name="items_quote_one_selected_per_item"
            ),
        ]

    def __str__(self):
        return f"{self.item.item_reference}: {self.supplier} {self.quoted_unit_price} {self.currency} {self.incoterm}"

    # -- validation ---------------------------------------------------------------

    def _invariant_errors(self):
        errors = {}
        if self.quoted_unit_price is None:
            errors["quoted_unit_price"] = "Enter the quoted unit price."
        elif _decimal(self.quoted_unit_price) < 0:
            errors["quoted_unit_price"] = "Cannot be negative."
        if self.currency not in Currency.values:
            errors["currency"] = "Choose the currency the supplier quoted in."
        if self.incoterm not in Incoterm.values:
            errors["incoterm"] = "Unknown Incoterm."
        if self.supplier_id is None:
            errors["supplier"] = "Which supplier sent this quote?"
        if self.original_document_id is None:
            errors["original_document"] = "Attach the supplier's quotation exactly as they sent it."
        else:
            document = (
                ItemAttachment._base_manager.filter(pk=self.original_document_id).values("item_id", "kind").first()
            )
            if (
                document is None
                or document["kind"] != AttachmentKind.QUOTATION
                or document["item_id"] != self.item_id
            ):
                errors["original_document"] = "The original document must be a QUOTATION attachment of this item."
        return errors

    def clean(self):
        super().clean()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        kwargs.pop("update_fields", None)
        self._check_guard()
        with transaction.atomic():
            item = Item._base_manager.select_related("tender").get(pk=self.item_id)
            assert_item_work_allowed(item.tender, "Saving a quote")
            errors = self._invariant_errors()
            if self._state.adding:
                if not _is_tenders_pm(self.entered_by, item.tender):
                    errors["entered_by"] = "Only this tender's Project Manager enters its quotes."
            else:
                stored = type(self)._base_manager.filter(pk=self.pk).values("status", *self.FIGURE_FIELDS).first()
                if stored and (
                    stored["item_id"] != self.item_id or stored["original_document_id"] != self.original_document_id
                ):
                    errors["item"] = "A quote never moves to another item or document."
                elif stored and stored["status"] != QuoteStatus.RECEIVED:
                    if any(stored[f] != getattr(self, f) for f in self.FIGURE_FIELDS):
                        raise ValidationError(
                            f"This quote is {stored['status']}; its figures are a record. "
                            "Enter a new quote instead.",
                            code="quote_locked",
                        )
            if errors:
                raise ValidationError(errors)
            super().save(*args, **kwargs)

    # -- the PM's decision ------------------------------------------------------------

    def select(self, user):
        """
        Make this the quote the item is costed on (§5 step 13). In one transaction:
        any previously selected quote goes back to RECEIVED, this one becomes
        SELECTED, its figures are copied onto the item, ``Item.selected_quote``
        points here, and the item re-costs (R10). Every changed row writes an
        AuditEntry attributed to ``user``. Returns the item.
        """
        tender = Item._base_manager.select_related("tender").get(pk=self.item_id).tender
        if not _is_tenders_pm(user, tender):
            raise PermissionDenied("Only this tender's PM selects the quote an item is costed on.")
        with transaction.atomic(), audit_actor(user):
            item = Item.objects.select_for_update().get(pk=self.item_id)
            assert_item_work_allowed(item.tender, "Selecting a quote")
            quote = type(self).objects.select_for_update().get(pk=self.pk)
            if quote.status == QuoteStatus.REJECTED:
                raise TransitionError("A rejected quote cannot be selected.", code="quote")
            if quote.status != QuoteStatus.SELECTED:
                for previous in type(self).objects.select_for_update().filter(
                    item_id=item.pk, status=QuoteStatus.SELECTED
                ):
                    previous.status = QuoteStatus.RECEIVED
                    with previous._system_write():
                        previous.save()
                quote.status = QuoteStatus.SELECTED
                with quote._system_write():
                    quote.save()
            for quote_field, item_field in Item.QUOTE_TO_ITEM:
                setattr(item, item_field, getattr(quote, quote_field))
            item.selected_quote = quote
            with item._system_write():
                item.save()
        self.refresh_from_db()
        return item

    def reject(self, user):
        """Set a RECEIVED quote aside. The selected one cannot be rejected: select another first."""
        tender = Item._base_manager.select_related("tender").get(pk=self.item_id).tender
        if not _is_tenders_pm(user, tender):
            raise PermissionDenied("Only this tender's PM rejects a quote.")
        with transaction.atomic(), audit_actor(user):
            assert_item_work_allowed(tender, "Rejecting a quote")
            quote = type(self).objects.select_for_update().get(pk=self.pk)
            if quote.status == QuoteStatus.SELECTED:
                raise TransitionError(
                    "The item is costed on this quote. Select another quote before rejecting it.", code="quote"
                )
            if quote.status == QuoteStatus.REJECTED:
                raise TransitionError("This quote is already rejected.", code="quote")
            quote.status = QuoteStatus.REJECTED
            with quote._system_write():
                quote.save()
        self.refresh_from_db()


@receiver(pre_delete, sender=Quote, dispatch_uid="items.guard_quote_delete")
def _guard_quote_delete(sender, instance, **kwargs):
    assert_item_work_allowed(instance.item.tender, "Deleting a quote")
    if sender._base_manager.filter(pk=instance.pk, status=QuoteStatus.SELECTED).exists():
        raise ValidationError("The item is costed on this quote; it cannot be deleted.")


# ---------------------------------------------------------------------------
# RFQ — a request for quotation as sent (§5 step 12, R8)
# ---------------------------------------------------------------------------


class RFQ(Auditable):
    """
    One RFQ email as sent to one supplier: which items it asked about, the
    registered ``documents.Document`` it was rendered from, the exact recipients
    and the Message-ID that links replies and reminders back to the tender (§10).
    Created only by ``items.services.rfq.send_rfq``. ``cc`` always holds the
    sales mailbox (R8): added in clean() and save(), and a database constraint.
    """

    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="rfqs")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="+")
    items = models.ManyToManyField(Item, related_name="rfqs")
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="+")
    to = models.JSONField(default=list)
    cc = models.JSONField(default=list)
    message_id = models.CharField(max_length=255, unique=True)
    reply_by = models.DateField(null=True, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-sent_at", "-id"]
        verbose_name = "RFQ"
        verbose_name_plural = "RFQs"
        constraints = [
            # R8, last line of defence (stored lower-case, see documents.rfq).
            models.CheckConstraint(condition=Q(cc__contains=[SALES_CC]), name="items_rfq_cc_sales"),
            models.CheckConstraint(condition=~Q(to=[]), name="items_rfq_has_recipient"),
        ]

    def __str__(self):
        return f"RFQ {self.tender.reference} to {self.supplier}"

    def _normalise_recipients(self):
        self.to = normalise_recipients(self.to)
        self.cc = with_required_cc(self.cc)  # R8: the system adds it; nothing removes it
        if not self.to:
            raise ValidationError({"to": "An RFQ needs a recipient."})

    def clean(self):
        super().clean()
        self._normalise_recipients()

    def save(self, *args, **kwargs):
        self._normalise_recipients()
        super().save(*args, **kwargs)


@receiver(m2m_changed, sender=RFQ.items.through, dispatch_uid="items.guard_rfq_items")
def _guard_rfq_items(sender, instance, action, reverse, pk_set, **kwargs):
    """An RFQ covers items of its own tender only."""
    if action not in ("pre_add", "pre_remove", "pre_clear"):
        return
    if reverse:
        raise ValidationError("Link items from the RFQ: rfq.items.add(...).")
    if action == "pre_add" and pk_set:
        if Item._base_manager.filter(pk__in=pk_set).exclude(tender_id=instance.tender_id).exists():
            raise ValidationError("An RFQ can cover only items of its own tender.")


# ---------------------------------------------------------------------------
# SupplierSuggestion — §5 step 11, §7
# ---------------------------------------------------------------------------


class SupplierSuggestion(GuardedWrites, Auditable):
    """
    An AI-proposed manufacturer for a specification. It is never a Supplier until
    the PM accepts it (R12 applies exactly as to extraction). The model's stated
    rationale and its source URLs are mandatory: an unverifiable suggestion is
    worse than none, because it looks authoritative.
    """

    tender = models.ForeignKey(Tender, on_delete=models.PROTECT, related_name="supplier_suggestions")
    items = models.ManyToManyField(Item, related_name="supplier_suggestions", help_text="One suggestion may cover a whole unit.")
    scope = models.CharField(max_length=16, choices=SourcingType.choices)
    suggested_name = models.CharField(max_length=255)
    country = models.CharField(max_length=100, blank=True)
    website = models.URLField(blank=True)
    rationale = models.TextField(help_text="Why the model says this candidate fits.")
    source_urls = models.JSONField(default=list, help_text="URLs the claim came from.")
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True, help_text="0 to 1.")
    status = models.CharField(
        max_length=16, choices=SuggestionStatus.choices, default=SuggestionStatus.SUGGESTED, editable=False
    )
    linked_supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="+", editable=False,
        help_text="Set on accept.",
    )
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    GUARDED_FIELDS = ("status", "linked_supplier")

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(scope__in=SourcingType.values), name="items_suggestion_scope_valid"),
            models.CheckConstraint(condition=Q(status__in=SuggestionStatus.values), name="items_suggestion_status_valid"),
            models.CheckConstraint(
                condition=Q(confidence__isnull=True) | Q(confidence__gte=0, confidence__lte=1),
                name="items_suggestion_confidence_range",
            ),
            models.CheckConstraint(condition=~Q(suggested_name=""), name="items_suggestion_named"),
            models.CheckConstraint(condition=~Q(rationale=""), name="items_suggestion_has_rationale"),
            # A supplier is linked exactly when the PM accepted.
            models.CheckConstraint(
                condition=(
                    Q(status=SuggestionStatus.ACCEPTED, linked_supplier__isnull=False)
                    | (~Q(status=SuggestionStatus.ACCEPTED) & Q(linked_supplier__isnull=True))
                ),
                name="items_suggestion_supplier_iff_accepted",
            ),
        ]

    def __str__(self):
        return f"{self.tender.reference}: {self.suggested_name} ({self.status})"

    def _invariant_errors(self):
        errors = {}
        if not (self.suggested_name or "").strip():
            errors["suggested_name"] = "Name the suggested supplier."
        if not (self.rationale or "").strip():
            errors["rationale"] = "Keep the model's stated reason: the PM cannot judge a suggestion without it."
        urls = self.source_urls
        if not isinstance(urls, list) or not urls or not all(
            isinstance(u, str) and u.startswith(("http://", "https://")) for u in urls
        ):
            errors["source_urls"] = "Keep at least one source URL (http/https) for the claim."
        if self.scope not in SourcingType.values:
            errors["scope"] = "Complete unit or separate part."
        if self.confidence is not None and not (0 <= _decimal(self.confidence) <= 1):
            errors["confidence"] = "Between 0 and 1."
        if self.requested_by_id is None or not (
            self.requested_by.is_active and self.requested_by.role == Role.PROJECT_MANAGER
        ):
            errors["requested_by"] = "Only a Project Manager asks for suggestions."
        return errors

    def clean(self):
        super().clean()
        errors = self._invariant_errors()
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._check_guard()
        with transaction.atomic():
            assert_item_work_allowed(self.tender, "Saving a supplier suggestion")
            errors = self._invariant_errors()
            if errors:
                raise ValidationError(errors)
            super().save(*args, **kwargs)

    # -- the PM's decision (R12) --------------------------------------------------------

    def _decide(self, user):
        if not _is_tenders_pm(user, self.tender):
            raise PermissionDenied("Only this tender's PM accepts or rejects a suggestion.")

    def accept(self, user, supplier=None):
        """The PM accepts: link the given Supplier, or the existing one with the same
        name, or create one. The suggestion records which."""
        self._decide(user)
        with transaction.atomic(), audit_actor(user):
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            assert_item_work_allowed(locked.tender, "Accepting a supplier suggestion")
            if locked.status != SuggestionStatus.SUGGESTED:
                raise TransitionError(f"This suggestion is already {locked.status}.", code="suggestion")
            if supplier is None:
                name = " ".join(locked.suggested_name.split())
                supplier = Supplier.objects.filter(name__iexact=name).first()
                if supplier is None:
                    extra = {"website": locked.website} if any(f.name == "website" for f in Supplier._meta.get_fields()) else {}
                    supplier = Supplier.objects.create(
                        name=name, country=locked.country,
                        notes=f"Accepted from an AI suggestion for {locked.tender.reference}: {locked.rationale}",
                        **extra,
                    )
            locked.status = SuggestionStatus.ACCEPTED
            locked.linked_supplier = supplier
            with locked._system_write():
                locked.save()
        self.refresh_from_db()
        return supplier

    def reject(self, user):
        self._decide(user)
        with transaction.atomic(), audit_actor(user):
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            assert_item_work_allowed(locked.tender, "Rejecting a supplier suggestion")
            if locked.status != SuggestionStatus.SUGGESTED:
                raise TransitionError(f"This suggestion is already {locked.status}.", code="suggestion")
            locked.status = SuggestionStatus.REJECTED
            with locked._system_write():
                locked.save()
        self.refresh_from_db()


@receiver(m2m_changed, sender=SupplierSuggestion.items.through, dispatch_uid="items.guard_suggestion_items")
def _guard_suggestion_items(sender, instance, action, reverse, pk_set, **kwargs):
    """A suggestion covers items of its own tender only, and only while work is allowed."""
    if action not in ("pre_add", "pre_remove", "pre_clear"):
        return
    if reverse:  # called from item.supplier_suggestions.add(...)
        raise ValidationError("Link items from the suggestion: suggestion.items.add(...).")
    assert_item_work_allowed(instance.tender, "Changing the items of a supplier suggestion")
    if action == "pre_add" and pk_set:
        foreign = Item._base_manager.filter(pk__in=pk_set).exclude(tender_id=instance.tender_id)
        if foreign.exists():
            raise ValidationError("A suggestion can cover only items of its own tender.")
