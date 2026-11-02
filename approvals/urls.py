"""URLs for approvals. Included from config/urls.py at /approvals/."""

from django.urls import path

from . import views

app_name = "approvals"

urlpatterns = [
    path("registration/", views.registration_queue, name="registration_queue"),
    path("registration/<int:pk>/", views.registration_decide, name="registration_decide"),
    path("submission/", views.submission_queue, name="submission_queue"),
    path("submission/<int:pk>/", views.submission_decide, name="submission_decide"),
    path("eligibility/", views.eligibility_queue, name="eligibility_queue"),
    path("eligibility/raise/<int:tender_pk>/", views.eligibility_raise, name="eligibility_raise"),
    path("eligibility/<int:pk>/", views.eligibility_review, name="eligibility_review"),
    path("funds/", views.fund_list, name="fund_list"),
    path("funds/new/<int:tender_pk>/", views.fund_new, name="fund_new"),
    path("funds/<int:pk>/", views.fund_detail, name="fund_detail"),
    path("funds/<int:pk>/decide/", views.fund_decide, name="fund_decide"),
    path("funds/<int:pk>/pay/", views.fund_pay, name="fund_pay"),
]
