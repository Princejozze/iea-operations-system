from django.contrib import admin

from .models import Tender, TenderAssignment, TenderOutcome


class TenderAssignmentInline(admin.TabularInline):
    model = TenderAssignment
    extra = 0
    can_delete = False
    fields = ("created_at", "from_pm", "to_pm", "assigned_by", "reason")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False  # only Tender.reassign() writes these (R13)


class TenderOutcomeInline(admin.StackedInline):
    model = TenderOutcome
    extra = 0
    can_delete = False
    readonly_fields = ("bid_opening_position", "award_status", "contract_sum", "contract_currency", "recorded_by")

    def has_add_permission(self, request, obj=None):
        return False  # only Tender.record_outcome() writes this


@admin.register(Tender)
class TenderAdmin(admin.ModelAdmin):
    """
    - No "add": a tender is created only by PDF upload (§5).
    - Status, reference, PM and the other system-set fields are read-only;
      they change only through the transition methods (R19) and reassign() (R13).
    - No delete: a tender is withdrawn, not deleted.
    """

    list_display = ("reference", "title", "client", "project_manager", "status", "submission_deadline")
    list_filter = ("status",)
    # R18: system reference, legacy reference, official number, client, title.
    search_fields = (
        "reference", "legacy_reference", "official_reference", "client__name", "client__short_name", "title",
    )
    list_select_related = ("client", "project_manager")
    readonly_fields = (
        "reference", "status", "previous_status", "exchange_rate_locked_at",
        "project_manager", "created_by", "created_at", "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("reference", "legacy_reference", "status", "previous_status", "project_manager")}),
        ("Confirmed tender details", {"fields": (
            "title", "client", "client_address", "official_reference", "category",
            "submission_deadline", "clarification_deadline",
            "bid_security_type", "site_visit_required", "site_visit_date",
            "special_requirements", "required_forms",
        )}),
        ("System", {"fields": ("exchange_rate_locked_at", "created_by", "created_at", "updated_at")}),
    )
    inlines = [TenderAssignmentInline, TenderOutcomeInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
