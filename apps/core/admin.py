from django.contrib import admin

from .admin_utils import OwnerAdminMixin
from .models import AuditLog, Notification


@admin.register(Notification)
class NotificationAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("title", "user", "type", "read", "created_at")
    list_filter = ("type", "read")
    search_fields = ("title", "message", "user__email")
    autocomplete_fields = ("user",)
    date_hierarchy = "created_at"


@admin.register(AuditLog)
class AuditLogAdmin(OwnerAdminMixin, admin.ModelAdmin):
    """Append-only trail: every field is visible, but entries cannot be edited or deleted (keeps the trail trustworthy)."""

    list_display = ("created_at", "action", "entity", "entity_id", "user")
    list_filter = ("action", "entity")
    search_fields = ("action", "entity", "entity_id", "user__email")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
