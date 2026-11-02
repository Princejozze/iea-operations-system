"""Forms for the QC audit screen. The model decides what is allowed; these only collect input."""

from django import forms

from .models import QCAuditLine, Verdict

# The auditor's words on screen; the stored values stay POSITIVE / NEGATIVE.
VERDICT_CHOICES = [(Verdict.POSITIVE, "Complies"), (Verdict.NEGATIVE, "Does not comply")]


class AuditLineForm(forms.ModelForm):
    """One audit line: what the supplier offered, the verdict, and the comment.

    ``specification_required`` is never editable here: it is what the tender asks for.
    The numeric variance is derived by the model on save; it is never a field.
    """

    verdict = forms.ChoiceField(choices=VERDICT_CHOICES, required=False, widget=forms.RadioSelect)

    class Meta:
        model = QCAuditLine
        fields = ("specification_offered", "verdict", "comments")
        widgets = {
            "specification_offered": forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
            "comments": forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
        }
        labels = {"specification_offered": "Offered", "comments": "Comment"}

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        pk = self.instance.pk
        for name, field in self.fields.items():
            if name != "verdict":
                field.widget.attrs["id"] = f"l{pk}-{name}"


class CompleteAuditForm(forms.Form):
    reason = forms.CharField(
        label="Reason for the PM", required=False, widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text="Used when the audit is negative. Left empty, the comments on the failed lines are sent.",
    )
