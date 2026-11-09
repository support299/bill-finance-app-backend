from django.contrib import admin, messages
from django.db.models import Exists, OuterRef, Subquery

from apps.core.admin_utils import OwnerAdminMixin, reveal_secret

from .models import GhlAgency, GhlInvoice, GhlInvoiceEvent, GhlInvoicePayment, GhlLocation, GhlMember, GhlOAuthToken
from .services.locations import ensure_location_token, refresh_location_details, sync_installed_locations


@admin.register(GhlLocation)
class GhlLocationAdmin(OwnerAdminMixin, admin.ModelAdmin):
    """Deactivating a location (active=False) blocks that subaccount's workspace sign-in."""

    list_display = ("name", "location_id", "company_id", "has_token", "token_expires_at", "active", "user", "last_access_at", "created_at")
    list_filter = ("active",)
    search_fields = ("name", "location_id", "company_id", "email", "user__email")
    autocomplete_fields = ("user",)
    actions = ["refresh_from_ghl", "sync_installed", "mint_tokens"]

    def get_queryset(self, request):
        tokens = GhlOAuthToken.objects.filter(location_id=OuterRef("location_id"), uninstalled_at__isnull=True)
        return super().get_queryset(request).annotate(_has_token=Exists(tokens), _token_expires=Subquery(tokens.values("expires_at")[:1]))

    @admin.display(description="Has own token", boolean=True, ordering="_has_token")
    def has_token(self, obj):
        return obj._has_token

    @admin.display(description="Token expires", ordering="_token_expires")
    def token_expires_at(self, obj):
        return obj._token_expires

    @admin.action(description="Create the location's own token from the agency token")
    def mint_tokens(self, request, queryset):
        ok = sum(ensure_location_token(loc.location_id) for loc in queryset)
        self.message_user(
            request,
            f"{ok} of {queryset.count()} location(s) have their own token.",
            level=messages.SUCCESS if ok == queryset.count() else messages.WARNING,
        )

    @admin.action(description="Refresh name and details from GoHighLevel")
    def refresh_from_ghl(self, request, queryset):
        for loc in queryset:
            refresh_location_details(loc)
        self.message_user(request, f"Refreshed {queryset.count()} location(s).")

    @admin.action(description="Import all installed locations from the agency")
    def sync_installed(self, request, queryset):
        try:
            result = sync_installed_locations()
        except Exception as exc:  # noqa: BLE001
            self.message_user(request, f"Could not import locations: {exc}", level=messages.ERROR)
        else:
            self.message_user(request, f"Imported {result['locations']} installed location(s).")


@admin.register(GhlOAuthToken)
class GhlOAuthTokenAdmin(OwnerAdminMixin, admin.ModelAdmin):
    """Read-only. Encrypted tokens are displayed as stored; decrypted values are shown to superusers (audited)."""

    list_display = ("location_name", "location_id", "company_id", "user_type", "expires_at", "uninstalled_at", "updated_at")
    list_filter = ("user_type",)
    search_fields = ("location_id", "company_id")
    readonly_fields = [
        "location_id",
        "company_id",
        "user_type",
        "scope",
        "minted_from_scope",
        "expires_at",
        "installed_at",
        "updated_at",
        "uninstalled_at",
        "access_token_encrypted",
        "access_token_decrypted",
        "refresh_token_encrypted",
        "refresh_token_decrypted",
    ]

    @admin.display(description="Location")
    def location_name(self, obj):
        loc = GhlLocation.objects.filter(location_id=obj.location_id).first() if obj.location_id else None
        return loc.name if loc else ("Agency (Company token)" if obj.user_type == "Company" else "—")

    @admin.display(description="Access token (decrypted)")
    def access_token_decrypted(self, obj):
        return reveal_secret(obj, "access_token_encrypted")

    @admin.display(description="Refresh token (decrypted)")
    def refresh_token_decrypted(self, obj):
        return reveal_secret(obj, "refresh_token_encrypted")

    def has_add_permission(self, request):
        return False


class GhlInvoicePaymentInline(admin.TabularInline):
    model = GhlInvoicePayment
    extra = 0
    can_delete = False
    fields = ("ghl_transaction_id", "amount", "amount_refunded", "fee", "status", "paid_at", "provider")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(GhlInvoice)
class GhlInvoiceAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("invoice_number", "location_id", "ledger", "issue_date", "due_date", "total", "balance", "status", "needs_review")
    list_filter = ("status", "needs_review", "currency")
    search_fields = ("invoice_number", "ghl_invoice_id", "location_id", "user__email", "ledger__label")
    date_hierarchy = "issue_date"
    autocomplete_fields = ("user", "ledger")
    inlines = [GhlInvoicePaymentInline]


@admin.register(GhlInvoicePayment)
class GhlInvoicePaymentAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = ("ghl_transaction_id", "invoice", "amount", "amount_refunded", "fee", "status", "paid_at")
    list_filter = ("status", "provider")
    search_fields = ("ghl_transaction_id", "invoice__invoice_number", "reference")
    autocomplete_fields = ("user", "invoice")


@admin.register(GhlInvoiceEvent)
class GhlInvoiceEventAdmin(OwnerAdminMixin, admin.ModelAdmin):
    list_display = (
        "created_at",
        "ghl_invoice_id",
        "source",
        "event_type",
        "previous_status",
        "new_status",
        "previous_balance",
        "new_balance",
    )
    list_filter = ("source", "new_status")
    search_fields = ("ghl_invoice_id", "event_type")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in GhlInvoiceEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GhlAgency)
class GhlAgencyAdmin(admin.ModelAdmin):
    list_display = ("name", "company_id", "last_access_at", "created_at")
    search_fields = ("name", "company_id")
    readonly_fields = ("user", "created_at", "last_access_at")


@admin.register(GhlMember)
class GhlMemberAdmin(admin.ModelAdmin):
    """Who may open a sub-account's accounting. Access empty = automatic (agency staff admin, sub-account admins edit)."""

    list_display = ("name", "email", "location", "ghl_type", "ghl_role", "access", "access_set_by", "last_seen_at")
    list_filter = ("access", "ghl_type", "ghl_role")
    search_fields = ("name", "email", "ghl_user_id", "location__name", "location__location_id")
    autocomplete_fields = ("location",)
    readonly_fields = ("first_seen_at", "last_seen_at")
