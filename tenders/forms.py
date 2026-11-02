"""Forms for the tender screens: upload (§5 step 2) and verification (§5 step 7, R12)."""

import datetime

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.utils import timezone

from accounts.models import Role
from core.models import Client, TenderCategory

from .models import BidSecurityType, TenderStatus as S

MAX_UPLOAD_MB = 50


class TenderUploadForm(forms.Form):
    pdf = forms.FileField(
        label="Tender PDF",
        help_text="The document as published on NeST.",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf,.pdf"}),
    )

    def clean_pdf(self):
        pdf = self.cleaned_data["pdf"]
        if pdf.size > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"That file is larger than {MAX_UPLOAD_MB} MB.")
        # create_from_upload checks the %PDF- header again; checking here gives a form error.
        head = pdf.read(5)
        pdf.seek(0)
        if head != b"%PDF-":
            raise forms.ValidationError("That file is not a PDF.")
        return pdf


class DateTimeLocalInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, attrs=None):
        super().__init__(attrs, format="%Y-%m-%dT%H:%M")


def _local(dt):
    return timezone.localtime(dt) if dt is not None and timezone.is_aware(dt) else dt


def draft_datetime(value):
    """An extracted "YYYY-MM-DDTHH:MM:SS" or "YYYY-MM-DD" (Tanzania local time) as a naive datetime.

    A date-only value becomes 00:00 on that day; the template tells the PM no time was stated.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(value.strip()).replace(tzinfo=None)
    except ValueError:
        return None


class TenderVerificationForm(forms.Form):
    """
    Every PM-confirmable field (Tender.CONFIRMED_FIELDS), pre-filled from the
    extraction draft. Nothing here reaches the tender until the PM submits and
    Tender.confirm_extraction accepts it (R12).

    The extracted client name is never turned into a Client row automatically:
    the PM picks an existing client, or types a new one deliberately.
    """

    official_reference = forms.CharField(label="Official tender number", max_length=100, required=False)
    title = forms.CharField(max_length=500, required=False)
    client = forms.ModelChoiceField(
        queryset=Client.objects.all(), required=False, empty_label="— choose a client —"
    )
    new_client_name = forms.CharField(
        label="…or add a new client", max_length=255, required=False,
        help_text="Only if the client is not in the list above.",
    )
    client_address = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    submission_deadline = forms.DateTimeField(widget=DateTimeLocalInput(), required=False)
    clarification_deadline = forms.DateTimeField(widget=DateTimeLocalInput(), required=False)
    bid_security_type = forms.ChoiceField(
        label="Bid security", choices=[("", "— choose —"), *BidSecurityType.choices], required=False
    )
    site_visit_required = forms.TypedChoiceField(
        choices=[("", "— choose —"), ("yes", "Yes"), ("no", "No")],
        coerce=lambda v: {"yes": True, "no": False}[v], empty_value=None, required=False,
    )
    site_visit_date = forms.DateTimeField(widget=DateTimeLocalInput(), required=False)
    special_requirements = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5}), required=False, help_text="One requirement per line."
    )
    required_forms = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5}), required=False, help_text="One form per line."
    )
    category = forms.ModelChoiceField(
        queryset=TenderCategory.objects.all(), required=False, empty_label="— choose a category —"
    )

    # Rendered in this order, each with its extraction evidence beside it.
    FIELD_ORDER = (
        "official_reference", "title", "client", "new_client_name", "client_address",
        "submission_deadline", "clarification_deadline", "bid_security_type",
        "site_visit_required", "site_visit_date", "special_requirements", "required_forms", "category",
    )
    # Form field -> key in TenderExtraction.fields.
    EXTRACTION_KEY = {"client": "client_name", "new_client_name": None, "category": None}

    def __init__(self, *args, tender, extracted=None, **kwargs):
        self.tender = tender
        self.extracted = extracted or {}
        super().__init__(*args, initial=self._initial(), **kwargs)
        for field in self.fields.values():
            css = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs.setdefault("class", css)

    @classmethod
    def extraction_key(cls, name):
        return cls.EXTRACTION_KEY.get(name, name)

    def _initial(self):
        """What the tender already holds, else the extraction's draft value."""
        t, x = self.tender, self.extracted

        def draft(key):
            return (x.get(key) or {}).get("value")

        def lines(key, stored):
            if stored:
                return stored
            value = draft(key) or []
            return "\n".join(i.get("text", "") for i in value if isinstance(i, dict) and i.get("text"))

        site_visit = t.site_visit_required if t.site_visit_required is not None else draft("site_visit_required")
        initial = {
            "official_reference": t.official_reference or draft("official_reference") or "",
            "title": t.title or draft("title") or "",
            "client": t.client_id or self._match_client(draft("client_name")),
            "client_address": t.client_address or draft("client_address") or "",
            "submission_deadline": _local(t.submission_deadline) or draft_datetime(draft("submission_deadline")),
            "clarification_deadline": _local(t.clarification_deadline) or draft_datetime(draft("clarification_deadline")),
            "bid_security_type": t.bid_security_type or draft("bid_security_type") or "",
            "site_visit_required": {True: "yes", False: "no"}.get(site_visit, ""),
            "site_visit_date": _local(t.site_visit_date) or draft_datetime(draft("site_visit_date")),
            "special_requirements": lines("special_requirements", t.special_requirements),
            "required_forms": lines("required_forms", t.required_forms),
            "category": t.category_id,
        }
        if not initial["client"] and draft("client_name"):
            initial["new_client_name"] = draft("client_name")
        return initial

    @staticmethod
    def _match_client(name):
        """An existing client whose name or short name matches the extracted one exactly (any case)."""
        name = " ".join((name or "").split())
        if not name:
            return None
        match = (
            Client.objects.annotate(n=Lower("name"), s=Lower("short_name"))
            .filter(n=name.lower()).first()
            or Client.objects.annotate(s=Lower("short_name")).filter(s=name.lower()).first()
        )
        return match.pk if match else None

    def clean(self):
        data = super().clean()
        client, new_name = data.get("client"), " ".join((data.get("new_client_name") or "").split())
        if client and new_name:
            self.add_error("new_client_name", "Choose an existing client or add a new one, not both.")
        data["new_client_name"] = new_name
        return data

    def resolve_client(self):
        """The chosen client, or the new one (reusing an existing row on a case-insensitive name match).

        Call inside the same transaction as confirm_extraction so a refused
        confirmation leaves no stray client behind.
        """
        client = self.cleaned_data.get("client")
        name = self.cleaned_data.get("new_client_name")
        if client or not name:
            return client
        existing = Client.objects.annotate(n=Lower("name")).filter(n=name.lower()).first()
        return existing or Client.objects.create(name=name)

    def verified_values(self, client):
        """Keyword arguments for Tender.confirm_extraction."""
        d = self.cleaned_data

        def lines(text):
            return "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())

        return {
            "title": d["title"].strip(),
            "client": client,
            "client_address": d["client_address"].strip(),
            "official_reference": d["official_reference"].strip(),
            "submission_deadline": d["submission_deadline"],
            "clarification_deadline": d["clarification_deadline"],
            "bid_security_type": d["bid_security_type"],
            "site_visit_required": d["site_visit_required"],
            "site_visit_date": d["site_visit_date"],
            "special_requirements": lines(d["special_requirements"]),
            "required_forms": lines(d["required_forms"]),
            "category": d["category"],
        }


# ---------------------------------------------------------------------------
# The MD's pricing screen (R22): the numbers only — never the formulas.
# ---------------------------------------------------------------------------

# field -> (label, how the chain uses it, unit). The "how" text is read-only by design:
# the formulas are fixed (§4), so the screen describes them and edits only the numbers.
RATE_FIELD_INFO = {
    "shipping_pct": ("Shipping", "% of import material, when freight is not in the price", "%"),
    "duty_pct": ("Duty", "% of CIF", "%"),
    "cnf_pct": ("C&F", "% of CIF", "%"),
    "labour_rate_tzs_per_hour": ("Labour rate", "TZS per labour hour", "TZS/h"),
    "overhead_pct": ("Overhead", "% of material + risk; also on labour", "%"),
    "margin_pct": ("Margin", "% of material + risk + overhead; also on labour", "%"),
    "maintenance_pct": ("Maintenance & guarantee", "% of material and labour selling", "%"),
    "cost_of_capital_pct": ("Cost of capital", "% of total material — stored, NOT in the total (§4)", "%"),
}


class CostingRatesForm(forms.Form):
    """One field per TenderCostingRates.RATE_FIELDS, mirroring the model's ranges.
    The model validates again in preview()/apply(); this gives friendly errors first."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, (label, how, unit) in RATE_FIELD_INFO.items():
            if unit == "%":
                field = forms.DecimalField(
                    label=label, max_digits=5, decimal_places=2, min_value=0, max_value=100,
                )
                step = "0.01"
            else:
                field = forms.DecimalField(label=label, max_digits=12, decimal_places=2, min_value=0)
                step = "100"
            field.help_text = how
            field.widget.attrs.update({
                "class": "form-control num", "inputmode": "decimal", "step": step, "autocomplete": "off",
            })
            self.fields[name] = field


# ---------------------------------------------------------------------------
# Reassignment (R13): the MD moves a tender to another PM
# ---------------------------------------------------------------------------


class ReassignForm(forms.Form):
    """Any active PM except the one who holds it now. Each option shows how many open
    tenders that PM already carries, so the MD can see the load before choosing."""

    new_pm = forms.ModelChoiceField(
        label="Move it to", queryset=None, empty_label="Choose a project manager…",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        label="Reason", required=False, max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text="Optional. Recorded in the assignment history and sent to both PMs.",
    )

    def __init__(self, *args, tender, **kwargs):
        super().__init__(*args, **kwargs)
        closed = (S.WON, S.LOST, S.WITHDRAWN)
        self.fields["new_pm"].queryset = (
            get_user_model().objects.filter(role=Role.PROJECT_MANAGER, is_active=True)
            .exclude(pk=tender.project_manager_id)
            .annotate(open_tenders=Count("tenders_managed", filter=~Q(tenders_managed__status__in=closed)))
            .order_by("first_name", "last_name", "email")
        )
        self.fields["new_pm"].label_from_instance = lambda u: (
            f"{u.get_full_name() or u.email} — {u.open_tenders} open tender{'' if u.open_tenders == 1 else 's'}"
        )
