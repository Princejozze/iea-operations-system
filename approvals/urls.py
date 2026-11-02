"""URLs for approvals. Included from config/urls.py at /approvals/."""

from django.urls import path

from . import views

app_name = "approvals"

urlpatterns = [
    path("registration/", views.registration_queue, name="registration_queue"),
    path("registration/<int:pk>/", views.registration_decide, name="registration_decide"),
]
