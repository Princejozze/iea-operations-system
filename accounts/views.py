from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy

from .forms import IEAPasswordChangeForm


class PasswordChangeView(auth_views.PasswordChangeView):
    """The one page a user with ``must_change_password`` can reach (R21)."""

    form_class = IEAPasswordChangeForm
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        response = super().form_valid(form)  # saves, keeps the session valid
        messages.success(self.request, "Your password has been changed.")
        return response
