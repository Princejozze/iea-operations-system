"""URLs for audits. Included from config/urls.py at /audits/."""

from django.urls import path

from . import views

app_name = "audits"

urlpatterns = [
    path("", views.queue, name="queue"),
    path("tender/<int:tender_pk>/", views.audit_detail, name="detail"),
    path("tender/<int:tender_pk>/start/", views.start, name="start"),
    path("tender/<int:tender_pk>/complete/", views.complete, name="complete"),
    path("line/<int:pk>/save/", views.line_save, name="line_save"),
]
