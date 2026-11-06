from django.db import models

from apps.core.models import OwnedModel, OwnedQuerySet, UUIDModel


class PlaidItem(OwnedModel):
    item_id = models.TextField(unique=True)
    institution_id = models.TextField(blank=True, null=True)
    institution_name = models.TextField(blank=True, null=True)
    access_token_encrypted = models.TextField()
    environment = models.CharField(max_length=20, default="sandbox")
    consented_at = models.DateTimeField(auto_now_add=True)
    last_sync_at = models.DateTimeField(blank=True, null=True)
    sync_cursor = models.TextField(blank=True, null=True)
    connection_status = models.CharField(max_length=30, default="good")
    error_code = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    initial_sync_done = models.BooleanField(default=False)
    # Transactions dated before this are ignored by the sync. Set when the user deletes a bank's history up to a date, so a
    # later reconnect (which replays the bank's full history) does not bring it back. Null = keep everything.
    history_from = models.DateField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=["user"])]


class BankAccount(OwnedModel):
    plaid_item = models.ForeignKey(PlaidItem, on_delete=models.CASCADE, related_name="accounts")
    plaid_account_id = models.TextField(unique=True)
    institution_id = models.TextField(blank=True, null=True)
    account_name = models.TextField(blank=True, null=True)
    official_name = models.TextField(blank=True, null=True)
    mask = models.CharField(max_length=10, blank=True, null=True)
    account_type = models.TextField(blank=True, null=True)
    account_subtype = models.TextField(blank=True, null=True)
    current_balance = models.DecimalField(max_digits=18, decimal_places=2, blank=True, null=True)
    available_balance = models.DecimalField(max_digits=18, decimal_places=2, blank=True, null=True)
    iso_currency_code = models.CharField(max_length=10, default="USD", blank=True, null=True)
    last_balance_update = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=["user"]), models.Index(fields=["plaid_item"])]


class SyncLog(UUIDModel):
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="+")
    plaid_item = models.ForeignKey(PlaidItem, null=True, blank=True, on_delete=models.CASCADE, related_name="sync_logs")
    sync_started_at = models.DateTimeField(auto_now_add=True)
    sync_completed_at = models.DateTimeField(blank=True, null=True)
    transactions_added = models.IntegerField(default=0)
    transactions_modified = models.IntegerField(default=0)
    transactions_removed = models.IntegerField(default=0)
    status = models.CharField(max_length=20, default="running")
    trigger_source = models.CharField(max_length=20, default="manual")  # connect | manual | webhook
    error_message = models.TextField(blank=True, null=True)

    objects = OwnedQuerySet.as_manager()

    class Meta:
        ordering = ["-sync_started_at"]
        indexes = [models.Index(fields=["user", "-sync_started_at"])]


class Reconciliation(OwnedModel):
    """Checking one bank/credit-card account against a statement.

    Balances are in *statement terms*: money you have (checking) or money you owe (credit/loan). While in progress the
    ticked ("cleared") transactions point at it; once completed they are locked against balance-affecting edits.
    """

    STATUSES = [("in_progress", "in_progress"), ("completed", "completed")]

    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="reconciliations")
    statement_date = models.DateField()
    beginning_balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    ending_balance = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=12, choices=STATUSES, default="in_progress")
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-statement_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["bank_account"], condition=models.Q(status="in_progress"), name="one_open_reconciliation_per_account"
            ),
            models.UniqueConstraint(fields=["bank_account", "statement_date"], name="reconciliation_account_date_uniq"),
        ]
