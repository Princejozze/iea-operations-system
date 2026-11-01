"""
Admin for items. Computed figures, status and references are read-only: money
is computed, never entered (R10), and status changes only through the model's
methods (§4). Saving here still passes through Item.save(), so R15 and R9a hold.
"""

from django.contrib import admin

from .models import ExchangeRate, Item, ItemAttachment, Quote, SupplierSuggestion


class ItemAttachmentInline(admin.TabularInline):
    model = ItemAttachment
    extra = 0


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("item_reference", "tender", "specification", "quantity", "status", "total_selling_price_tzs")
    list_filter = ("status",)
    search_fields = ("item_reference", "tender__reference", "specification", "make_brand", "model_part_number")
    list_select_related = ("tender",)
    inlines = [ItemAttachmentInline]

    def get_readonly_fields(self, request, obj=None):
        computed = [
            f.name for f in Item._meta.concrete_fields
            if not f.editable or f.name in ("item_reference", "status")
        ]
        return [*computed, *(["tender"] if obj else [])]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ("currency", "rate_to_tzs", "as_of", "source")
    list_filter = ("currency",)


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ("item", "supplier", "unit_price", "currency", "incoterm", "received_at")


@admin.register(SupplierSuggestion)
class SupplierSuggestionAdmin(admin.ModelAdmin):
    """Look, don't touch: suggestions are created by discovery and decided by the PM."""

    list_display = ("tender", "suggested_name", "scope", "status", "confidence")
    list_filter = ("status", "scope")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
