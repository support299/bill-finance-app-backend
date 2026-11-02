from django.contrib import admin

from apps.core.admin_utils import OwnerAdminMixin, reveal_secret

from .models import BankAccount, PlaidItem, Reconciliation, SyncLog


class BankAccountInline(admin.TabularInline):
    model = BankAccount
    extra = 0
    can_delete = False
    fields = ("account_name", "mask", "account_type", "account_subtype", "current_balance", "available_balance")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PlaidItem)
class PlaidItemAdmin(OwnerAdminMixin, admin.ModelAdmin):
    """Everything is visible. The encrypted token is read-only; the decrypted token is shown to superusers (audited)."""

    list_display = ("institution_name", "user", "connection_status", "environment", "initial_sync_done", "last_sync_at", "created_at")
    list_filter = ("connection_status", "environment", "initial_sync_done")
    search_fields = ("institution_name", "item_id", "user__email")
    autocomplete_fields = ("user",)
    readonly_fields = ("access_token_encrypted", "access_token_decrypted", "consented_at")
    inlines = [BankAccountInline]

    @admin.display(description="Access token (decrypted)")
    def access_token_decrypted(self, obj):
        return reveal_secret(obj, "access_token_encrypted")


@admin.register(BankAccount)
class BankAccountAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("account_name", "mask", "account_type", "current_balance", "iso_currency_code", "plaid_item", "user")
    list_filter = ("account_type", "iso_currency_code")
    search_fields = ("account_name", "official_name", "mask", "plaid_account_id", "user__email")
    autocomplete_fields = ("user", "plaid_item")


@admin.register(SyncLog)
class SyncLogAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = (
        "sync_started_at",
        "plaid_item",
        "status",
        "trigger_source",
        "transactions_added",
        "transactions_modified",
        "transactions_removed",
    )
    list_filter = ("status", "trigger_source")
    search_fields = ("plaid_item__institution_name", "user__email", "error_message")
    date_hierarchy = "sync_started_at"
    readonly_fields = [f.name for f in SyncLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Reconciliation)
class ReconciliationAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("bank_account", "statement_date", "ending_balance", "status", "completed_at", "user")
    list_filter = ("status",)
    search_fields = ("bank_account__account_name", "user__email")
    autocomplete_fields = ("user", "bank_account")
