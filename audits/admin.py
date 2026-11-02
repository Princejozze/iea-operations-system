from django.contrib import admin

from .models import QCAudit, QCAuditLine


class QCAuditLineInline(admin.TabularInline):
    model = QCAuditLine
    extra = 0
    fields = ("item", "specification_required", "specification_offered", "variance", "verdict", "comments")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(QCAudit)
class QCAuditAdmin(admin.ModelAdmin):
    """
    Read-only. Audits are opened by QCAudit.objects.start() and closed by
    QCAudit.complete(), so R1, R9, R9a and the tender's status cannot be
    edited out of step from here. The verdict is a person's decision (§14).
    """

    list_display = ("tender", "auditor", "verdict", "completed_at", "created_at")
    list_filter = ("verdict",)
    search_fields = ("tender__reference", "auditor__email")
    list_select_related = ("tender", "auditor")
    inlines = [QCAuditLineInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
