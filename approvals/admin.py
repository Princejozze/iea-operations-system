"""
Read-only. An Approval is a signed decision, an EligibilityReview is decided by
the MD through ``decide()``, and a FundRequest changes status only through its
own methods — so the admin may look but never add, change or delete (§4, R7).
"""

from django.contrib import admin

from .models import Approval, EligibilityReview, FundRequest


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Approval)
class ApprovalAdmin(ReadOnlyAdmin):
    list_display = ("tender", "kind", "decision", "decided_by", "decided_at")
    list_filter = ("kind", "decision")
    search_fields = ("tender__reference", "tender__title")


@admin.register(EligibilityReview)
class EligibilityReviewAdmin(ReadOnlyAdmin):
    list_display = ("tender", "raised_by", "md_decision", "decided_by", "decided_at")
    list_filter = ("md_decision",)
    search_fields = ("tender__reference", "requirement")


@admin.register(FundRequest)
class FundRequestAdmin(ReadOnlyAdmin):
    list_display = ("tender", "purpose", "amount", "currency", "status", "requested_by", "paid_at")
    list_filter = ("purpose", "status")
    search_fields = ("tender__reference", "payment_reference")
