from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied

from .forms import UserProvisionForm
from .models import EmailAlias, User
from .provisioning import provision_user, reissue_temporary_password


class EmailAliasInline(admin.TabularInline):
    model = EmailAlias
    extra = 0
    fields = ("address",)


@admin.register(User)
class IEAUserAdmin(admin.ModelAdmin):
    """
    R21 in the admin:
    - "Add user" asks for name, email and role only. The system generates and
      emails the temporary password; the creator never sees or sets it.
    - Only EXEC or ADMIN may add users, superuser or not.
    - No admin screen sets another user's password. A lost or expired one is
      replaced with the "Reissue temporary password" action.
    - Users are deactivated, never deleted (their audit entries PROTECT them).
    """

    add_form = UserProvisionForm
    list_display = ("email", "first_name", "last_name", "role", "rank", "is_active", "must_change_password")
    list_filter = ("role", "is_active", "must_change_password")
    search_fields = ("email", "first_name", "last_name", "email_aliases__address")
    ordering = ("first_name", "last_name")
    inlines = [EmailAliasInline]
    actions = ["reissue_password"]
    readonly_fields = (
        "rank",
        "must_change_password",
        "temporary_password_expires_at",
        "is_superuser",
        "last_login",
        "date_joined",
    )
    fieldsets = (
        (None, {"fields": ("email", "first_name", "last_name", "role", "rank")}),
        ("Access", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Provisioning (R21)", {"fields": ("must_change_password", "temporary_password_expires_at")}),
        ("History", {"fields": ("last_login", "date_joined")}),
    )
    filter_horizontal = ("groups", "user_permissions")

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = self.add_form
            kwargs["fields"] = UserProvisionForm.Meta.fields
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return ((None, {"fields": UserProvisionForm.Meta.fields}),)
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        return () if obj is None else self.readonly_fields

    def get_inlines(self, request, obj):
        return [] if obj is None else self.inlines

    def has_add_permission(self, request):
        return request.user.can_provision_accounts and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
        else:
            provision_user(created_by=request.user, user=obj)
            self.message_user(
                request,
                f"Account created. A temporary password has been emailed to {obj.email}; "
                "it expires in 7 days.",
                messages.SUCCESS,
            )

    @admin.action(description="Reissue temporary password (emails the user)")
    def reissue_password(self, request, queryset):
        if not request.user.can_provision_accounts:
            raise PermissionDenied
        for user in queryset:
            reissue_temporary_password(issued_by=request.user, user=user)
        self.message_user(request, f"Reissued {queryset.count()} temporary password(s).")
