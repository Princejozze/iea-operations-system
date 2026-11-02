"""Forms for the MD's decisions and for fund requests. The model layer is the authority;
the signature is verified by Approval.objects.sign (§4)."""

from decimal import Decimal

from django import forms

from tenders.models import Currency

from .models import ApprovalDecision, FundRequestPurpose


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
