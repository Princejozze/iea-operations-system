"""
Forms for the item sheet and quote entry.

The model layer is the authority (§4 "Enforcement"): these forms collect and
shape input, call the model, and put its refusals back on the right field. They
never re-implement a rule.
"""

from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from suppliers.models import Supplier
from tenders.models import Currency

from .models import AttachmentKind, Incoterm, Item, SourcingType


def add_model_errors(form, exc):
    """Put a model-layer ValidationError (field dict or plain messages) on the form."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            target = name if name in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for message in exc.messages:
            form.add_error(None, message)


def error_messages(form):
    """Every error on a form as short 'Field: message' strings, for a one-line status."""
    out = []
    for name, errors in form.errors.items():
        label = "" if name == "__all__" else f"{form.fields[name].label or name}: "
        out += [f"{label}{e}" for e in errors]
    return out


MAX_UPLOAD_MB = 15
UPLOAD_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx", ".xls", ".xlsx")


def validate_upload(upload):
    """A supplier document or brochure: a known type, and small enough to cross a slow link."""
    name = (upload.name or "").lower()
    if not name.endswith(UPLOAD_EXTENSIONS):
        raise ValidationError("Attach a PDF, an image or an Office document (%s)." % ", ".join(UPLOAD_EXTENSIONS))
    if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f"That file is {upload.size / 1048576:.1f} MB; the limit is {MAX_UPLOAD_MB} MB.")
    if upload.size == 0:
        raise ValidationError("That file is empty.")


def _plain(value):
    """A Decimal without trailing zeros or an exponent: Decimal('2671.0000') -> '2671'."""
    if value is None:
        return None
    text = format(Decimal(value).normalize(), "f")
    return text


def _style(form):
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, (forms.CheckboxInput, forms.RadioSelect)):
            w.attrs.setdefault("class", "form-check-input")
        elif isinstance(w, forms.Select):
            w.attrs.setdefault("class", "form-select")
        elif isinstance(w, forms.FileInput):
            w.attrs.setdefault("class", "form-control")
        else:
            w.attrs.setdefault("class", "form-control")


# ---------------------------------------------------------------------------
# One row of the item sheet
# ---------------------------------------------------------------------------


class ItemRowForm(forms.ModelForm):
    """
    Everything the PM edits on an item. Money is never a field here (R10): the
    PM enters the supplier's *unit price*, and the costing chain computes every
    other figure. ``unit_price`` fills both ``unit_cost`` (what the supplier
    charges) and ``fob_unit_price`` (the chain's import input), exactly as
    selecting a quote does (``Item.QUOTE_TO_ITEM``).

    While a quote is selected the item is costed on it and the figures that came
    from it are locked: change them by entering and selecting a new quote.
    """

    unit_price = forms.DecimalField(
        label="Unit price", required=False, min_value=Decimal(0), max_digits=18, decimal_places=4,
        widget=forms.NumberInput(attrs={"inputmode": "decimal", "step": "any", "min": "0"}),
    )

    # A text box over one shared <datalist> on the page, not a <select> per item: with ~60 suppliers
    # and 101 items, a select in every card would be ~6,000 <option> tags down a phone's data plan.
    supplier_name = forms.CharField(
        label="Supplier", required=False, max_length=255,
        widget=forms.TextInput(attrs={"list": "suppliers-dl", "autocomplete": "off", "placeholder": "Start typing"}),
    )

    # Fields a selected quote supplies.
    QUOTE_FIELDS = (
        "unit_price", "currency", "incoterm", "shipping_applies", "supplier_name",
        "country_of_origin", "make_brand", "model_part_number",
    )

    # Rendered on demand (items/_item_more.html) to keep a 101-item page light. A form posted
    # WITHOUT them must leave them alone: an absent field reads as empty to a ModelForm, and
    # saving a price would otherwise blank the category, make and model.
    MORE_FIELDS = (
        "specification", "unit_of_measure", "category", "make_brand", "model_part_number", "country_of_origin",
        "sourcing_type", "shipping_applies", "local_unit_cost", "risk_pct", "labour_hours_per_unit",
    )

    class Meta:
        model = Item
        fields = (
            "specification", "quantity", "unit_of_measure", "category", "make_brand", "model_part_number",
            "country_of_origin", "sourcing_type", "currency", "incoterm", "shipping_applies",
            "local_unit_cost", "risk_pct", "labour_hours_per_unit",
        )
        widgets = {
            "specification": forms.Textarea(attrs={"rows": 3}),
            "quantity": forms.NumberInput(attrs={"inputmode": "numeric", "min": "1"}),
            "local_unit_cost": forms.NumberInput(attrs={"inputmode": "decimal", "step": "any", "min": "0"}),
            "risk_pct": forms.NumberInput(attrs={"inputmode": "decimal", "step": "any", "min": "0", "max": "100"}),
            "labour_hours_per_unit": forms.NumberInput(attrs={"inputmode": "decimal", "step": "any", "min": "0"}),
        }
        labels = {
            "unit_of_measure": "Unit", "make_brand": "Make / brand", "model_part_number": "Model / part no.",
            "country_of_origin": "Origin", "sourcing_type": "Sourcing", "shipping_applies": "Imported",
            "local_unit_cost": "Local cost / unit (TZS)", "risk_pct": "Risk %", "labour_hours_per_unit": "Labour h / unit",
        }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        item = self.instance
        if self.is_bound and "more_loaded" not in self.data:
            for name in self.MORE_FIELDS:
                del self.fields[name]
        # Show 2671, not 2671.0000: the database keeps four decimals, the PM types what the quote says.
        self.initial["unit_price"] = _plain(item.unit_cost)
        for name in ("local_unit_cost", "risk_pct", "labour_hours_per_unit"):
            if name in self.fields:
                self.initial[name] = _plain(getattr(item, name))
        self.fields["supplier_name"].initial = item.supplier.name if item.supplier_id else ""
        self.initial["supplier_name"] = item.supplier.name if item.supplier_id else ""
        self.fields["currency"].choices = [("", "—"), *Currency.choices]
        if "sourcing_type" in self.fields:
            self.fields["sourcing_type"].choices = [("", "—"), *SourcingType.choices]
        self.locked = item.selected_quote_id is not None
        if self.locked:
            for name in self.QUOTE_FIELDS:
                if name in self.fields:
                    self.fields[name].disabled = True
        _style(self)
        for name, field in self.fields.items():
            field.widget.attrs["id"] = f"i{item.pk}-{name}"

    def clean_supplier_name(self):
        name = " ".join((self.cleaned_data.get("supplier_name") or "").split())
        self._supplier = None
        if name:
            self._supplier = Supplier.objects.filter(name__iexact=name).first()
            if self._supplier is None:
                raise ValidationError(f"There is no supplier called “{name}”. Pick one from the list, or add it when you enter a quote.")
        return name

    def _post_clean(self):
        if not self.locked and "supplier_name" in self.cleaned_data:
            self.instance.supplier = self._supplier
        # Put the price on the instance first, so Item.clean() judges price and currency together.
        if not self.locked and "unit_price" in self.cleaned_data:
            price = self.cleaned_data["unit_price"]
            self.instance.unit_cost = price
            self.instance.fob_unit_price = price
        super()._post_clean()


class NewItemForm(forms.Form):
    """Add an item by hand (§5: the PM adds items when the schedule parse missed some)."""

    specification = forms.CharField(label="Specification", widget=forms.Textarea(attrs={"rows": 3}))
    quantity = forms.IntegerField(label="Quantity", min_value=1, initial=1,
                                  widget=forms.NumberInput(attrs={"inputmode": "numeric", "min": "1"}))
    unit_of_measure = forms.CharField(label="Unit", required=False, max_length=50, initial="Each")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class AttachmentForm(forms.Form):
    kind = forms.ChoiceField(label="Kind", choices=AttachmentKind.choices)
    file = forms.FileField(label="File", validators=[validate_upload])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


# ---------------------------------------------------------------------------
# Quote entry (§5 step 13)
# ---------------------------------------------------------------------------


class QuoteForm(forms.Form):
    """The supplier's original plus the figures. Both are mandatory (§5 step 13)."""

    supplier = forms.ModelChoiceField(label="Supplier", queryset=Supplier.objects.none(), required=False,
                                      empty_label="— choose —")
    new_supplier_name = forms.CharField(label="…or a new supplier", required=False, max_length=255,
                                        help_text="Only if the supplier is not in the list.")
    new_supplier_country = forms.CharField(label="Supplier country", required=False, max_length=100)
    original_document = forms.FileField(
        label="Supplier's quotation, exactly as sent", validators=[validate_upload],
        help_text="The evidence. A price with no document behind it cannot be defended at audit.",
    )
    quoted_unit_price = forms.DecimalField(
        label="Unit price", min_value=Decimal(0), max_digits=18, decimal_places=4,
        widget=forms.NumberInput(attrs={"inputmode": "decimal", "step": "any", "min": "0"}),
    )
    currency = forms.ChoiceField(label="Currency", choices=[("", "—"), *Currency.choices])
    incoterm = forms.ChoiceField(label="Incoterm", choices=Incoterm.choices, initial=Incoterm.CIF)
    shipping_applies = forms.BooleanField(label="Imported (shipping, duty and C&F apply)", required=False, initial=True)
    country_of_origin = forms.CharField(label="Country of origin", required=False, max_length=100)
    make_brand = forms.CharField(label="Make / brand (as offered)", required=False, max_length=255)
    model_part_number = forms.CharField(label="Model / part number (as offered)", required=False, max_length=255)
    specification_offered = forms.CharField(
        label="Specification offered", required=False, widget=forms.Textarea(attrs={"rows": 4}),
        help_text="What the supplier commits to. The QC auditor reads this beside what the tender requires.",
    )
    lead_time_days = forms.IntegerField(label="Lead time (days)", required=False, min_value=0,
                                        widget=forms.NumberInput(attrs={"inputmode": "numeric", "min": "0"}))
    valid_until = forms.DateField(label="Valid until", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    select_now = forms.BooleanField(label="Cost this item on this quote", required=False)

    def __init__(self, *args, item, **kwargs):
        super().__init__(*args, **kwargs)
        self.item = item
        self.fields["supplier"].queryset = Supplier.objects.order_by("name")
        if not self.is_bound:
            # Start from what the item already says, so the PM types only what differs.
            self.initial.update(
                currency=item.currency, incoterm=item.incoterm, country_of_origin=item.country_of_origin,
                make_brand=item.make_brand, model_part_number=item.model_part_number,
                select_now=item.selected_quote_id is None,
            )
        _style(self)

    def clean(self):
        data = super().clean()
        if not data.get("supplier") and not (data.get("new_supplier_name") or "").strip():
            self.add_error("supplier", "Choose the supplier, or type a new one.")
        if data.get("supplier") and (data.get("new_supplier_name") or "").strip():
            self.add_error("new_supplier_name", "Choose a supplier from the list or add a new one, not both.")
        return data

    def resolve_supplier(self):
        """The chosen supplier, or the new one — reusing an existing row on a case-insensitive name match."""
        chosen = self.cleaned_data.get("supplier")
        if chosen:
            return chosen
        name = " ".join(self.cleaned_data["new_supplier_name"].split())
        existing = Supplier.objects.filter(name__iexact=name).first()
        if existing:
            return existing
        return Supplier.objects.create(name=name, country=self.cleaned_data.get("new_supplier_country", ""))

    def figures(self):
        """Keyword arguments for ``items.services.quotes.enter_quote``."""
        d = self.cleaned_data
        return dict(
            quoted_unit_price=d["quoted_unit_price"], currency=d["currency"], incoterm=d["incoterm"],
            shipping_applies=d["shipping_applies"], country_of_origin=d["country_of_origin"],
            make_brand=d["make_brand"], model_part_number=d["model_part_number"],
            specification_offered=d["specification_offered"], lead_time_days=d["lead_time_days"],
            valid_until=d["valid_until"], notes=d["notes"],
        )


class QuoteSheetUploadForm(forms.Form):
    """One filled sheet = one supplier = one original document (items.services.quote_sheet)."""

    supplier = forms.ModelChoiceField(label="Supplier", queryset=Supplier.objects.none(), empty_label="— choose —")
    sheet = forms.FileField(label="Filled quote sheet (.xlsx)")
    original_document = forms.FileField(
        label="Supplier's quotation, exactly as sent", validators=[validate_upload],
        help_text="One file: it becomes the evidence behind every quote on the sheet.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.order_by("name")
        _style(self)
