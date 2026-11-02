"""URLs for tenders. Included from config/urls.py at /tenders/."""

from django.urls import path

from . import views

app_name = "tenders"

urlpatterns = [
    path("", views.tender_list, name="list"),
    path("upload/", views.tender_upload, name="upload"),
    path("<int:pk>/", views.tender_detail, name="detail"),
    path("<int:pk>/status/", views.tender_status, name="status"),
    path("<int:pk>/verify/", views.tender_verify, name="verify"),
]
