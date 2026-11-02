"""URLs for items. Included from config/urls.py at /items/."""

from django.urls import path

from . import views

app_name = "items"

urlpatterns = [
    # The item sheet and the moves the PM makes from it.
    path("tender/<int:pk>/", views.sheet, name="sheet"),
    path("tender/<int:pk>/start-costing/", views.start_costing, name="start_costing"),
    path("tender/<int:pk>/submit-for-audit/", views.submit_for_audit, name="submit_for_audit"),
    path("tender/<int:pk>/items/add/", views.item_add, name="item_add"),
    path("tender/<int:pk>/quote-sheet.xlsx", views.quote_sheet_download, name="quote_sheet_download"),
    path("tender/<int:pk>/quote-sheet/", views.quote_sheet_upload, name="quote_sheet_upload"),
    # One item: inline save, delete, attachments, quotes.
    path("item/<int:pk>/save/", views.item_save, name="item_save"),
    path("item/<int:pk>/more/", views.item_more, name="item_more"),
    path("item/<int:pk>/delete/", views.item_delete, name="item_delete"),
    path("item/<int:pk>/attachments/add/", views.attachment_add, name="attachment_add"),
    path("item/<int:pk>/quotes/", views.quotes, name="quotes"),
    path("attachment/<int:pk>/", views.attachment_download, name="attachment_download"),
    path("attachment/<int:pk>/delete/", views.attachment_delete, name="attachment_delete"),
    path("quote/<int:pk>/select/", views.quote_select, name="quote_select"),
    path("quote/<int:pk>/reject/", views.quote_reject, name="quote_reject"),
]
