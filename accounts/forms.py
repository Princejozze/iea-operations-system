from django import forms
from django.contrib.auth.forms import PasswordChangeForm

from .models import User


class IEAPasswordChangeForm(PasswordChangeForm):
    """
    Password change that also completes R21: clears ``must_change_password``
    and the temporary-password expiry in the same save, so the AuditEntry
    records the first password change (with the hash redacted).
    """

    def clean(self):
        cleaned = super().clean()
        old, new = cleaned.get("old_password"), cleaned.get("new_password1")
        if old and new and old == new:
            raise forms.ValidationError("Choose a new password, not the temporary one.")
        return cleaned

    def save(self, commit=True):
        self.user.set_password(self.cleaned_data["new_password1"])
        self.user.must_change_password = False
        self.user.temporary_password_expires_at = None
        if commit:
            self.user.save()
        return self.user


class UserProvisionForm(forms.ModelForm):
    """Admin 'add user' form: name, email and role only — no password field (R21)."""

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "role")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.Meta.fields:
            self.fields[name].required = True
