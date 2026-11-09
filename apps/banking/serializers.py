from rest_framework import serializers

from .models import BankAccount, PlaidItem, SyncLog


class PlaidItemSerializer(serializers.ModelSerializer):
    transaction_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = PlaidItem
        # access_token_encrypted and sync_cursor are never exposed.
        fields = [
            "id",
            "institution_name",
            "institution_id",
            "connection_status",
            "error_message",
            "last_sync_at",
            "environment",
            "created_at",
            "initial_sync_done",
            "history_from",
            "transaction_count",
        ]


class PlaidItemUpdateSerializer(serializers.Serializer):
    history_from = serializers.DateField(allow_null=True)


class BankAccountSerializer(serializers.ModelSerializer):
    plaid_item_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = BankAccount
        exclude = ["user", "plaid_item"]


class SyncLogSerializer(serializers.ModelSerializer):
    plaid_item_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = SyncLog
        exclude = ["user", "plaid_item"]


class LinkTokenRequestSerializer(serializers.Serializer):
    item_id = serializers.UUIDField(required=False)
    history_days = serializers.IntegerField(required=False, min_value=1, max_value=730)  # how far back Plaid pulls (default 730)


class ExchangeSerializer(serializers.Serializer):
    public_token = serializers.CharField(min_length=10, max_length=500)
    institution_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    institution_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    history_days = serializers.IntegerField(required=False, min_value=1, max_value=730)


class SyncRequestSerializer(serializers.Serializer):
    item_id = serializers.UUIDField(required=False)


class ReconciliationCreateSerializer(serializers.Serializer):
    account_id = serializers.UUIDField()
    statement_date = serializers.DateField()
    ending_balance = serializers.DecimalField(max_digits=18, decimal_places=2)
    beginning_balance = serializers.DecimalField(max_digits=18, decimal_places=2, required=False)


class ReconciliationClearSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=5000)
    cleared = serializers.BooleanField()


class ReconciliationUpdateSerializer(serializers.Serializer):
    statement_date = serializers.DateField(required=False)
    ending_balance = serializers.DecimalField(max_digits=18, decimal_places=2, required=False)
    beginning_balance = serializers.DecimalField(max_digits=18, decimal_places=2, required=False)


class CsvImportSerializer(serializers.Serializer):
    csv_text = serializers.CharField(max_length=2_000_000, trim_whitespace=False)
    account_id = serializers.UUIDField(required=False)
    institution_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    account_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    account_type = serializers.ChoiceField(choices=["depository", "credit", "loan", "investment", "other"], default="depository")
    mask = serializers.CharField(max_length=10, required=False, allow_blank=True)
    invert_sign = serializers.BooleanField(default=False)
    dry_run = serializers.BooleanField(default=False)
