"""Forms for the MD's decisions. The signature is verified by Approval.objects.sign (§4)."""

from django import forms

from .models import ApprovalDecision


class RegistrationDecisionForm(forms.Form):
    """R15: approve or decline a tender's registration, signed with typed full name + password."""

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
