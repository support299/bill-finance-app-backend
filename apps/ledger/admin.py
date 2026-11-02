from django.contrib import admin

from apps.core.admin_utils import OwnerAdminMixin, reveal_secret

from .models import Category, CategoryRule, Ledger, LedgerTag, Project, Transaction, VendorTaxProfile


@admin.register(Category)
class CategoryAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("name", "type", "is_system", "user", "color")
    list_filter = ("type", "is_system")
    search_fields = ("name", "user__email")
    autocomplete_fields = ("user", "parent_category")
    ordering = ("type", "name")


@admin.register(CategoryRule)
class CategoryRuleAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("keyword", "match_type", "category", "ledger", "priority", "active", "source", "user")
    list_filter = ("match_type", "active", "source")
    search_fields = ("keyword", "user__email")
    autocomplete_fields = ("user", "category", "ledger")
    ordering = ("priority",)


@admin.register(LedgerTag)
class LedgerTagAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("name", "user")
    search_fields = ("name", "user__email")
    autocomplete_fields = ("user",)


@admin.register(Ledger)
class LedgerAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("label", "account_class", "contact_name", "contact_email", "ghl_contact_id", "user")
    list_filter = ("account_class",)
    search_fields = ("label", "contact_name", "contact_email", "ghl_contact_id", "user__email")
    autocomplete_fields = ("user",)
    filter_horizontal = ("tags",)


@admin.register(Transaction)
class TransactionAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = (
        "transaction_date",
        "merchant_name",
        "amount",
        "currency",
        "gaap_type",
        "user_category",
        "ledger",
        "source",
        "status",
        "is_hidden",
        "user",
    )
    list_filter = ("source", "status", "gaap_type", "accounting_status", "pending", "is_hidden", "is_transfer", "removed_at")
    search_fields = ("merchant_name", "description", "original_description", "plaid_transaction_id", "external_ref", "user__email")
    date_hierarchy = "transaction_date"
    autocomplete_fields = ("user", "user_category", "ledger", "plaid_item")
    list_select_related = ("user", "user_category", "ledger")
    list_per_page = 50


@admin.register(VendorTaxProfile)
class VendorTaxProfileAdmin(OwnerAdminMixin, admin.ModelAdmin):
    """Everything is visible. The TIN is stored encrypted: ciphertext is read-only, the decrypted TIN is shown to superusers (audited)."""

    list_display = ("ledger", "is_1099_vendor", "form_type", "legal_name", "tax_classification", "tin_last4", "w9_received_at", "user")
    list_filter = ("is_1099_vendor", "form_type", "tax_classification")
    search_fields = ("legal_name", "business_name", "ledger__label", "user__email")
    autocomplete_fields = ("user", "ledger")
    readonly_fields = ("tin_encrypted", "tin_decrypted", "tin_last4")

    @admin.display(description="TIN (decrypted)")
    def tin_decrypted(self, obj):
        return reveal_secret(obj, "tin_encrypted")


@admin.register(Project)
class ProjectAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("name", "ledger", "status", "budget", "user")
    list_filter = ("status",)
    search_fields = ("name", "user__email")
    autocomplete_fields = ("user", "ledger")
