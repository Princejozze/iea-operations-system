"""Forms for the MD's decisions and for fund requests. The model layer is the authority;
the signature is verified by Approval.objects.sign (§4)."""

from decimal import Decimal

from django import forms

from suppliers.dedup import find_existing
from suppliers.models import Supplier
from tenders.models import Currency

from .models import ApprovalDecision, EligibilityDecision, FundRequestPurpose


class SignedDecisionForm(forms.Form):
    """An MD decision signed with typed full name + password (§4 "Signatures")."""

    decision = forms.ChoiceField(choices=ApprovalDecision.choices, widget=forms.RadioSelect)
    comments = forms.CharField(
        label="Reason", required=False, widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text="Required when declining — the PM is told this reason.",
    )
    typed_name = forms.CharField(
        label="Type your full name to sign", max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "name"}),
    )
    password = forms.CharField(
        label="Re-enter your password",
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "current-password"}),
    )

    def clean(self):
        data = super().clean()
        if data.get("decision") == ApprovalDecision.DECLINED and not (data.get("comments") or "").strip():
            self.add_error("comments", "Give the reason for declining. The PM is told it.")
        return data


class RegistrationDecisionForm(SignedDecisionForm):
    """R15: approve or decline a tender's registration."""


class SubmissionDecisionForm(SignedDecisionForm):
    """R3 / R9: approve or decline a tender's submission. A decline returns it to costing."""

    comments = forms.CharField(
        label="Reason", required=False, widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text="Required when declining — the PM is told this reason and the tender returns to costing.",
    )


# ---------------------------------------------------------------------------
# Fund requests
# ---------------------------------------------------------------------------


class FundRequestForm(forms.Form):
    purpose = forms.ChoiceField(label="What is it for?", choices=FundRequestPurpose.choices,
                                initial=FundRequestPurpose.NEST_APPLICATION_FEE)
    amount = forms.DecimalField(
        label="Amount", min_value=Decimal("0.01"), max_digits=18, decimal_places=2,
        widget=forms.NumberInput(attrs={"inputmode": "decimal", "step": "0.01", "min": "0.01"}),
    )
    currency = forms.ChoiceField(label="Currency", choices=Currency.choices, initial=Currency.TZS)
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-select" if isinstance(field.widget, forms.Select) else "form-control")


class FundDecisionForm(forms.Form):
    """The MD's decision on a fund request. A decline needs its reason (the PM is told it)."""

    DECISIONS = (("APPROVE", "Approve"), ("DECLINE", "Decline"))
    decision = forms.ChoiceField(choices=DECISIONS, widget=forms.RadioSelect)
    reason = forms.CharField(label="Reason", required=False, widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
                             help_text="Required when declining.")

    def clean(self):
        data = super().clean()
        if data.get("decision") == "DECLINE" and not (data.get("reason") or "").strip():
            self.add_error("reason", "Give the reason for declining. The PM is told it.")
        return data


class MarkPaidForm(forms.Form):
    payment_reference = forms.CharField(
        label="Payment reference", max_length=100,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
        help_text="The bank or mobile-money reference for the payment you made.",
    )


# ---------------------------------------------------------------------------
# Eligibility reviews (R16)
# ---------------------------------------------------------------------------

OTHER = "__other__"


class RaiseEligibilityForm(forms.Form):
    """The PM names the special requirement IEA does not meet. The tender's own confirmed
    special requirements are offered first, so the review quotes the tender's words."""

    requirement_choice = forms.ChoiceField(label="Which requirement does IEA not meet?", widget=forms.RadioSelect)
    requirement_other = forms.CharField(
        label="The requirement, in the tender's words", required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )
    pm_notes = forms.CharField(
        label="What would it take?", required=False,
        widget=forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
        help_text="What IEA lacks, and any partner, certificate or fix you know of. The MD reads this before ruling.",
    )

    def __init__(self, *args, tender, **kwargs):
        super().__init__(*args, **kwargs)
        lines = [ln.strip(" •-\t") for ln in (tender.special_requirements or "").splitlines()]
        lines = [ln for ln in lines if ln]
        self.fields["requirement_choice"].choices = [(str(i), ln) for i, ln in enumerate(lines)] + [
            (OTHER, "Another requirement (type it below)" if lines else "Type the requirement below"),
        ]
        self._lines = lines
        if not lines:
            self.initial.setdefault("requirement_choice", OTHER)

    def clean(self):
        data = super().clean()
        choice = data.get("requirement_choice")
        if choice == OTHER:
            text = " ".join((data.get("requirement_other") or "").split())
            if not text:
                self.add_error("requirement_other", "Type the requirement IEA does not meet.")
            data["requirement"] = text
        elif choice is not None:
            data["requirement"] = self._lines[int(choice)]
        return data


class EligibilityDecisionForm(forms.Form):
    """R16: the MD's ruling. JOINT_VENTURE names a partner, picked or typed; WITHDRAW is
    final, so it must be confirmed; every ruling except PROCEED explains itself to the PM."""

    md_decision = forms.ChoiceField(label="Your ruling", widget=forms.RadioSelect)
    jv_partner = forms.ModelChoiceField(
        label="Joint venture partner", required=False, queryset=None, empty_label="Choose a firm…",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    new_partner_name = forms.CharField(
        label="…or a firm not yet on the supplier list", required=False, max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    decision_notes = forms.CharField(
        label="Notes for the PM", required=False, widget=forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
        help_text="Required unless you proceed: what to remediate, the JV terms, or why the tender is withdrawn.",
    )
    confirm_withdraw = forms.BooleanField(
        label="I understand withdrawing is final: the tender cannot be reopened.", required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["md_decision"].choices = EligibilityDecision.choices
        self.fields["jv_partner"].queryset = Supplier.objects.order_by("name")

    def clean(self):
        data = super().clean()
        D = EligibilityDecision
        decision = data.get("md_decision")
        partner, new_name = data.get("jv_partner"), " ".join((data.get("new_partner_name") or "").split())
        data["new_partner_name"] = new_name
        if decision == D.JOINT_VENTURE:
            if not partner and not new_name:
                self.add_error("jv_partner", "Name the joint venture partner: choose one, or type a new firm.")
            elif partner and new_name:
                self.add_error("new_partner_name", "Choose a firm from the list or type a new one, not both.")
        else:  # a partner on any other ruling would be ignored; never store one (model rule)
            data["jv_partner"], data["new_partner_name"] = None, ""
        if decision and decision != D.PROCEED and not (data.get("decision_notes") or "").strip():
            self.add_error("decision_notes", "Tell the PM why. Every ruling except proceed needs a note.")
        if decision == D.WITHDRAW and not data.get("confirm_withdraw"):
            self.add_error("confirm_withdraw", "Tick to confirm the tender is withdrawn for good.")
        return data

    def resolve_partner(self):
        """The chosen firm, or the typed one — reusing an existing supplier whose name matches
        after normalisation (case, punctuation, Ltd/GmbH…), so no duplicate is created."""
        d = self.cleaned_data
        if d.get("jv_partner") or not d.get("new_partner_name"):
            return d.get("jv_partner")
        name = d["new_partner_name"]
        existing = Supplier.objects.filter(name__iexact=name).first() or find_existing(name, Supplier.objects.all())
        return existing or Supplier.objects.create(name=name)
