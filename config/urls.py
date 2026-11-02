"""
URL configuration for the IEA Central Operations & Procurement
Management System.

There is deliberately no sign-up URL: accounts are created only by the MD or a
System Admin (R21).
"""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts.views import PasswordChangeView
from dashboards.views import dashboard

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
    path("account/password/", PasswordChangeView.as_view(), name="password_change"),
    path("", dashboard, name="dashboard"),
    # One include() per app, in CLAUDE.md §1 order. Each app owns its urls.py, so
    # adding routes never edits this file.
    path("documents/", include("documents.urls")),
    path("suppliers/", include("suppliers.urls")),
    path("tenders/", include("tenders.urls")),
    path("items/", include("items.urls")),
    path("audits/", include("audits.urls")),
    path("approvals/", include("approvals.urls")),
    path("postaward/", include("postaward.urls")),
    path("comms/", include("comms.urls")),
    path("tasks/", include("tasks.urls")),
    path("dashboards/", include("dashboards.urls")),
]
