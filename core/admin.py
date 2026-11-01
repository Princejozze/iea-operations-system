from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse

from .models import AuditEntry, Client, CostingDefaults, TenderCategory


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    """
    View-only for everyone, superusers included (R7).

    These overrides return False unconditionally; Django's superuser bypass
    lives in the default implementations, so overriding them removes it.
    """

    list_display = ("timestamp", "actor", "action", "model", "object_id", "object_repr")
    list_filter = ("action", "model")
    search_fields = ("object_id", "object_repr", "model")
    date_hierarchy = "timestamp"
    list_select_related = ("actor",)
    actions = None  # removes "delete selected"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "updated_at")
    search_fields = ("name", "short_name")


@admin.register(TenderCategory)
class TenderCategoryAdmin(admin.ModelAdmin):
    """New categories are added here — no migration needed (§8)."""

    list_display = ("name", "updated_at")
    search_fields = ("name",)


@admin.register(CostingDefaults)
class CostingDefaultsAdmin(admin.ModelAdmin):
    """
    The company costing rates (CLAUDE.md section 4). One row, edited in place.

    Only the Managing Director and a System Admin may view or change it, decided
    by role rather than by Django permission flags, so that a superuser whose
    role is anything else is refused too. Every change is audited under R7 with
    the old and new values and the person who made it. The row can be neither
    added a second time nor deleted.
    """

    EDITOR_ROLES = ("EXEC", "ADMIN")  # role codes; core must not import accounts
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("Imports", {"fields": ("shipping_pct", "duty_pct", "cnf_pct")}),
        ("Margins", {"fields": ("overhead_pct", "margin_pct", "maintenance_pct")}),
        ("Labour", {"fields": ("labour_rate_tzs_per_hour",)}),
        ("Tax", {"fields": ("vat_pct",)}),
        ("Cost of capital (unresolved)", {"fields": ("cost_of_capital_pct",)}),
        ("History", {"fields": ("created_at", "updated_at")}),
    )

    def _is_editor(self, request):
        user = request.user
        return bool(user.is_active and getattr(user, "role", None) in self.EDITOR_ROLES)

    def has_module_permission(self, request):
        return self._is_editor(request)

    def has_view_permission(self, request, obj=None):
        return self._is_editor(request)

    def has_change_permission(self, request, obj=None):
        return self._is_editor(request)

    def has_add_permission(self, request):
        return self._is_editor(request) and not CostingDefaults.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        row = CostingDefaults.objects.filter(pk=CostingDefaults.SINGLETON_PK).first()
        if row is not None and self.has_change_permission(request, row):
            return redirect(reverse("admin:core_costingdefaults_change", args=[row.pk]))
        return super().changelist_view(request, extra_context)
