from django.contrib import admin

from .models import Document


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    """
    Read-only. Documents are created by the system (documents.services) so that
    the render, the registry entry and the R8 cc cannot be edited out of step.
    """

    list_display = ("title", "kind", "related_reference", "created_by", "created_at")
    list_filter = ("kind",)
    search_fields = ("title", "related_reference", "subject")
    date_hierarchy = "created_at"
    list_select_related = ("created_by",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
