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
            "transaction_count",
        ]


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


class ExchangeSerializer(serializers.Serializer):
    public_token = serializers.CharField(min_length=10, max_length=500)
    institution_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    institution_name = serializers.CharField(max_length=200, required=False, allow_blank=True)


class SyncRequestSerializer(serializers.Serializer):
    item_id = serializers.UUIDField(required=False)
