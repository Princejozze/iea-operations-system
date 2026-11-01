from django.contrib import admin

from .models import Supplier


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "is_oem", "country", "contact_person", "email", "updated_at")
    list_filter = ("is_oem", "country")
    search_fields = ("name", "contact_person", "email", "country")
