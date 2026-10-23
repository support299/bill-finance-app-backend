from django.db import models
from django.db.models import Q

from apps.core.models import OwnedModel, TimestampedModel

GAAP_TYPES = [
    "revenue",
    "cogs",
    "operating_expense",
    "other_income",
    "other_expense",
    "asset",
    "liability",
    "equity",
    "transfer",
]
GAAP_CHOICES = [(t, t) for t in GAAP_TYPES]
ACCOUNT_CLASSES = ["current_asset", "noncurrent_asset", "current_liability", "noncurrent_liability", "equity"]
ACCOUNTING_STATUSES = ["accounts_receivable", "received", "accounts_payable", "paid"]


class Category(TimestampedModel):
    """user is NULL for the shared system chart of accounts."""

    user = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    name = models.TextField()
    type = models.CharField(max_length=20, choices=GAAP_CHOICES)
    parent_category = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")
    icon = models.TextField(blank=True, null=True)
    color = models.TextField(blank=True, null=True)
    is_system = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(fields=["name", "type"], condition=Q(user__isnull=True), name="categories_system_name_uniq"),
        ]


class Ledger(OwnedModel):
    label = models.TextField()
    contact_name = models.TextField(blank=True, null=True)
    contact_email = models.TextField(blank=True, null=True)
    contact_phone = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    account_class = models.CharField(max_length=30, choices=[(c, c) for c in ACCOUNT_CLASSES])
    ghl_contact_id = models.TextField(blank=True, null=True)
    tags = models.ManyToManyField("LedgerTag", related_name="ledgers", blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "label"], name="ledgers_user_label_uniq"),
            models.UniqueConstraint(
                fields=["user", "ghl_contact_id"], condition=Q(ghl_contact_id__isnull=False), name="ledgers_user_ghl_contact_uniq"
            ),
        ]


class LedgerTag(OwnedModel):
    name = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "name"], name="ledger_tags_user_name_uniq")]


class CategoryRule(OwnedModel):
    keyword = models.TextField()
    match_type = models.CharField(
        max_length=20, default="contains", choices=[(m, m) for m in ("contains", "starts_with", "exact", "regex")]
    )
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.CASCADE, related_name="rules")
    ledger = models.ForeignKey(Ledger, null=True, blank=True, on_delete=models.SET_NULL, related_name="rules")
    priority = models.IntegerField(default=100)
    active = models.BooleanField(default=True)
    source = models.CharField(max_length=20, default="user")

    class Meta:
        indexes = [models.Index(fields=["user", "active", "priority"])]


class Transaction(OwnedModel):
    SOURCES = [("plaid", "plaid"), ("manual", "manual"), ("ghl", "ghl")]
    STATUSES = [(s, s) for s in ("pending", "posted", "paid", "void")]

    plaid_item = models.ForeignKey("banking.PlaidItem", null=True, blank=True, on_delete=models.CASCADE, related_name="transactions")
    plaid_account_id = models.TextField(blank=True, null=True)
    plaid_transaction_id = models.TextField(unique=True, null=True, blank=True)
    ledger = models.ForeignKey(Ledger, null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions")
    merchant_name = models.TextField(blank=True, null=True)
    original_description = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)  # signed: negative = money out
    currency = models.CharField(max_length=10, default="USD", blank=True, null=True)
    transaction_date = models.DateField()
    authorized_date = models.DateField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    pending = models.BooleanField(default=False)
    payment_channel = models.TextField(blank=True, null=True)
    transaction_type = models.TextField(blank=True, null=True)
    merchant_entity_id = models.TextField(blank=True, null=True)
    merchant_logo_url = models.TextField(blank=True, null=True)
    website = models.TextField(blank=True, null=True)
    city = models.TextField(blank=True, null=True)
    region = models.TextField(blank=True, null=True)
    postal_code = models.TextField(blank=True, null=True)
    country = models.TextField(blank=True, null=True)
    plaid_personal_finance_primary_category = models.TextField(blank=True, null=True)
    plaid_personal_finance_detailed_category = models.TextField(blank=True, null=True)
    plaid_category_confidence = models.TextField(blank=True, null=True)
    user_category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions")
    category_source = models.TextField(blank=True, null=True)
    categorization_confidence = models.TextField(blank=True, null=True)
    transaction_direction = models.TextField(blank=True, null=True)
    gaap_type = models.CharField(max_length=20, choices=GAAP_CHOICES, blank=True, null=True)
    is_income = models.BooleanField(default=False)
    is_expense = models.BooleanField(default=False)
    is_transfer = models.BooleanField(default=False)
    is_recurring = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    is_demo = models.BooleanField(default=False)
    source = models.CharField(max_length=10, choices=SOURCES, default="plaid")
    status = models.CharField(max_length=10, choices=STATUSES, default="posted")
    accounting_status = models.CharField(max_length=20, choices=[(s, s) for s in ACCOUNTING_STATUSES], blank=True, null=True)
    external_ref = models.TextField(blank=True, null=True)
    removed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "external_ref"], name="transactions_user_external_ref_key")]
        indexes = [
            models.Index(fields=["user", "-transaction_date"]),
            models.Index(fields=["plaid_account_id"]),
            models.Index(fields=["user", "is_income", "is_expense", "is_transfer"]),
            models.Index(fields=["user", "removed_at"]),
            models.Index(fields=["ledger", "-transaction_date"]),
            models.Index(
                fields=["user", "accounting_status", "due_date"],
                condition=Q(accounting_status__isnull=False),
                name="idx_tx_accounting_status",
            ),
        ]


class VendorTaxProfile(OwnedModel):
    ledger = models.OneToOneField(Ledger, on_delete=models.CASCADE, related_name="vendor_profile")
    is_1099_vendor = models.BooleanField(default=False)
    form_type = models.CharField(max_length=10, default="nec", choices=[("nec", "nec"), ("misc", "misc")])
    misc_box = models.CharField(
        max_length=30,
        blank=True,
        null=True,
        choices=[(b, b) for b in ("rents", "royalties", "other_income", "medical", "attorney_proceeds")],
    )
    legal_name = models.TextField(blank=True, null=True)
    business_name = models.TextField(blank=True, null=True)
    tax_classification = models.CharField(
        max_length=30,
        blank=True,
        null=True,
        choices=[
            (c, c)
            for c in (
                "individual",
                "llc_single",
                "llc_partnership",
                "llc_c_corp",
                "llc_s_corp",
                "partnership",
                "c_corp",
                "s_corp",
                "trust_estate",
                "other",
            )
        ],
    )
    is_attorney = models.BooleanField(default=False)
    tin_type = models.CharField(max_length=10, blank=True, null=True, choices=[(t, t) for t in ("ssn", "ein", "itin")])
    tin_encrypted = models.TextField(blank=True, null=True)
    tin_last4 = models.CharField(max_length=4, blank=True, null=True)
    address_line1 = models.TextField(blank=True, null=True)
    address_line2 = models.TextField(blank=True, null=True)
    city = models.TextField(blank=True, null=True)
    state = models.TextField(blank=True, null=True)
    postal_code = models.TextField(blank=True, null=True)
    w9_received_at = models.DateField(blank=True, null=True)
    backup_withholding = models.BooleanField(default=False)
