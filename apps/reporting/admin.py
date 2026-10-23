from django.contrib import admin

from apps.core.admin_utils import OwnerAdminMixin

from .models import SavedReport


@admin.register(SavedReport)
class SavedReportAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("name", "report_type", "date_from", "date_to", "user", "created_at")
    list_filter = ("report_type",)
    search_fields = ("name", "user__email")
    autocomplete_fields = ("user",)
