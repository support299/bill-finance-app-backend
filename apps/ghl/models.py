from django.db import models
from django.db.models import Q

from apps.core.models import OwnedModel, UUIDModel


class GhlLocation(UUIDModel):
    """A GoHighLevel subaccount (location) and the hidden tenant user that owns its data."""

    location_id = models.TextField(unique=True)
    user = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    name = models.TextField(blank=True, null=True)  # the subaccount's name in GoHighLevel
    company_id = models.TextField(blank=True, null=True)  # the agency it belongs to
    email = models.TextField(blank=True, null=True)
    phone = models.TextField(blank=True, null=True)
    details = models.JSONField(default=dict, blank=True)  # raw location payload from GoHighLevel
    details_synced_at = models.DateTimeField(blank=True, null=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_access_at = models.DateTimeField(blank=True, null=True)
    invoices_synced_at = models.DateTimeField(blank=True, null=True)
    media_folder_id = models.TextField(blank=True, null=True)  # the receipts folder in this subaccount's media library

    def __str__(self):
        return f"{self.name or '(unnamed)'} · {self.location_id}"


class GhlOAuthToken(UUIDModel):
    location_id = models.TextField(blank=True, null=True)
    company_id = models.TextField(blank=True, null=True)
    user_type = models.TextField(blank=True, null=True)
    access_token_encrypted = models.TextField()
    refresh_token_encrypted = models.TextField()
    scope = models.TextField(blank=True, null=True)
    # For location tokens minted from the agency token: the agency scope at that moment (detects permission changes).
    minted_from_scope = models.TextField(blank=True, null=True)
    expires_at = models.DateTimeField()
    installed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    uninstalled_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [
            # Portable equivalent of UNIQUE NULLS NOT DISTINCT (company_id, location_id), which needs PostgreSQL 15+.
            models.UniqueConstraint(fields=["location_id"], condition=Q(location_id__isnull=False), name="ghl_token_location_uniq"),
            models.UniqueConstraint(
                fields=["company_id"], condition=Q(location_id__isnull=True, company_id__isnull=False), name="ghl_token_agency_uniq"
            ),
        ]


class GhlInvoice(OwnedModel):
    location_id = models.TextField()
    ghl_invoice_id = models.TextField()
    ghl_contact_id = models.TextField(blank=True, null=True)
    ledger = models.ForeignKey("ledger.Ledger", null=True, blank=True, on_delete=models.SET_NULL, related_name="ghl_invoices")
    invoice_number = models.TextField(blank=True, null=True)
    issue_date = models.DateField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    currency = models.TextField(blank=True, null=True)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    amount_refunded = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    ghl_status = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, default="open")
    needs_review = models.BooleanField(default=False)
    review_reason = models.TextField(blank=True, null=True)
    raw = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "ghl_invoice_id"], name="ghl_invoices_user_invoice_uniq")]


class GhlInvoicePayment(OwnedModel):
    invoice = models.ForeignKey(GhlInvoice, on_delete=models.CASCADE, related_name="payments")
    ghl_transaction_id = models.TextField()
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    amount_refunded = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    fee = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(blank=True, null=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    reference = models.TextField(blank=True, null=True)
    provider = models.TextField(blank=True, null=True)
    raw = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "ghl_transaction_id"], name="ghl_payments_user_tx_uniq")]


class GhlInvoiceEvent(OwnedModel):
    invoice = models.ForeignKey(GhlInvoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    ghl_invoice_id = models.TextField()
    source = models.TextField()
    event_type = models.TextField(blank=True, null=True)
    previous_status = models.TextField(blank=True, null=True)
    new_status = models.TextField(blank=True, null=True)
    previous_balance = models.DecimalField(max_digits=18, decimal_places=2, blank=True, null=True)
    new_balance = models.DecimalField(max_digits=18, decimal_places=2, blank=True, null=True)
    payload = models.JSONField(default=dict)


class GhlAgency(UUIDModel):
    """A GoHighLevel agency (company) and the hidden user that represents its admins' support sessions."""

    company_id = models.TextField(unique=True)
    user = models.OneToOneField("accounts.User", on_delete=models.CASCADE, related_name="+")
    name = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_access_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.name or 'Agency'} · {self.company_id}"


class GhlMember(UUIDModel):
    """A GoHighLevel user who can (or cannot) open a sub-account's accounting.

    Rows appear on first SSO sign-in (or when an administrator sets access before that). ``access`` NULL means automatic:
    agency staff administer, sub-account admins edit, other sub-account users have no access (see services/sso.py).
    """

    ACCESS = [(a, a) for a in ("none", "viewer", "editor", "admin")]

    location = models.ForeignKey(GhlLocation, on_delete=models.CASCADE, related_name="members")
    ghl_user_id = models.TextField()
    name = models.TextField(blank=True, null=True)
    email = models.TextField(blank=True, null=True)
    ghl_type = models.TextField(blank=True, null=True)  # agency | location
    ghl_role = models.TextField(blank=True, null=True)  # admin | user
    access = models.CharField(max_length=8, choices=ACCESS, blank=True, null=True)  # NULL = automatic
    access_set_by = models.TextField(blank=True, null=True)
    access_set_at = models.DateTimeField(blank=True, null=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["location", "ghl_user_id"], name="ghl_member_location_user_uniq")]

    def __str__(self):
        return f"{self.name or self.email or self.ghl_user_id} @ {self.location_id}"
