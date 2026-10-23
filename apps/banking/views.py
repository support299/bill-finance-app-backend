import logging

from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.crypto import decrypt_secret, encrypt_secret
from apps.core.services import audit, notify
from apps.ledger.models import Transaction

from . import serializers as s
from .models import BankAccount, PlaidItem, SyncLog
from .services import plaid_client as plaid
from .services.sync import refresh_item_status, sync_item

logger = logging.getLogger(__name__)


def _plaid_failure(exc: plaid.PlaidError) -> ValidationError:
    return ValidationError({"detail": plaid.friendly_plaid_error(exc.error_code)})


class PlaidConfigView(APIView):
    def get(self, request):
        return Response(
            {
                "configured": plaid.is_configured(),
                "environment": settings.PLAID_ENV.lower(),
                "webhook_configured": bool(settings.PLAID_WEBHOOK_URL),
            }
        )


class LinkTokenView(APIView):
    """Short-lived Link token; only link_token reaches the browser."""

    def post(self, request):
        ser = s.LinkTokenRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        access_token = None
        if ser.validated_data.get("item_id"):
            item = PlaidItem.objects.for_user(request.user).filter(pk=ser.validated_data["item_id"]).first()
            if not item:
                raise NotFound("Connection not found.")
            access_token = decrypt_secret(item.access_token_encrypted)
        try:
            res = plaid.create_link_token(str(request.user.id), settings.PLAID_WEBHOOK_URL or None, access_token)
        except plaid.PlaidError as exc:
            raise _plaid_failure(exc)
        return Response({"link_token": res["link_token"]})


class ExchangeView(APIView):
    """Exchanges the public token server-side and kicks off the first sync."""

    def post(self, request):
        ser = s.ExchangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        try:
            res = plaid.exchange_public_token(d["public_token"])
        except plaid.PlaidError as exc:
            raise _plaid_failure(exc)
        item_id, access_token = res["item_id"], res["access_token"]

        existing = PlaidItem.objects.filter(item_id=item_id).first()
        if existing and existing.user_id != request.user.id:
            raise ValidationError({"detail": "This connection belongs to another account."})

        fields = {
            "user": request.user,
            "institution_id": d.get("institution_id") or None,
            "institution_name": d.get("institution_name") or "Bank",
            "access_token_encrypted": encrypt_secret(access_token),
            "environment": plaid.plaid_env(),
            "connection_status": "good",
            "error_code": None,
            "error_message": None,
        }
        item, _ = PlaidItem.objects.update_or_create(item_id=item_id, defaults=fields)
        audit(request.user, "bank_connected", "plaid_item", str(item.id), {"institution": item.institution_name})
        notify(request.user, "Bank connected", f"{item.institution_name} is connected. Importing transactions…", "success")
        outcome = sync_item(item.id, trigger="connect")
        return Response({"item_id": str(item.id), **outcome.as_dict()})


class SyncView(APIView):
    def post(self, request):
        ser = s.SyncRequestSerializer(data=request.data or {})
        ser.is_valid(raise_exception=True)
        items = PlaidItem.objects.for_user(request.user)
        if ser.validated_data.get("item_id"):
            items = items.filter(pk=ser.validated_data["item_id"])
        from django.utils import timezone

        out = {"synced": 0, "added": 0, "modified": 0, "removed": 0, "skipped": 0}
        for item in items:
            # Simple rate limit: at most one manual sync per item per minute.
            if item.last_sync_at and (timezone.now() - item.last_sync_at).total_seconds() < 60:
                out["skipped"] += 1
                continue
            r = sync_item(item.id)
            out["synced"] += 1
            out["added"] += r.added
            out["modified"] += r.modified
            out["removed"] += r.removed
        return Response(out)


class RefreshView(APIView):
    def post(self, request, item_id):
        item = get_object_or_404(PlaidItem.objects.for_user(request.user), pk=item_id)
        try:
            plaid.transactions_refresh(decrypt_secret(item.access_token_encrypted))
        except plaid.PlaidError as exc:
            if exc.error_code != "PRODUCTS_NOT_SUPPORTED":
                raise _plaid_failure(exc)
        return Response(sync_item(item.id).as_dict())


class ItemStatusView(APIView):
    def post(self, request, item_id):
        item = get_object_or_404(PlaidItem.objects.for_user(request.user), pk=item_id)
        refresh_item_status(item.id)
        return Response({"ok": True})


class ItemDetailView(APIView):
    def delete(self, request, item_id):
        item = get_object_or_404(PlaidItem.objects.for_user(request.user), pk=item_id)
        delete_data = request.query_params.get("delete_data", "false").lower() in ("true", "1")
        try:
            plaid.item_remove(decrypt_secret(item.access_token_encrypted))
        except Exception:  # noqa: BLE001 - provider-side removal is best effort
            logger.info("Plaid item/remove failed for %s", item.pk)
        if not delete_data:
            # Keep imported history, detach it from the connection.
            Transaction.objects.filter(plaid_item=item).update(plaid_item=None)
        name = item.institution_name or "Bank"
        item_pk = str(item.id)
        item.delete()
        audit(request.user, "bank_disconnected", "plaid_item", item_pk, {"delete_data": delete_data})
        notify(request.user, "Bank disconnected", f"{name} was disconnected.", "info")
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccountsView(APIView):
    def get(self, request):
        items = (
            PlaidItem.objects.for_user(request.user)
            .annotate(transaction_count=Count("transactions", filter=Q(transactions__removed_at__isnull=True)))
            .order_by("created_at")
        )
        accounts = BankAccount.objects.for_user(request.user).order_by("account_name")
        return Response(
            {
                "items": s.PlaidItemSerializer(items, many=True).data,
                "accounts": s.BankAccountSerializer(accounts, many=True).data,
            }
        )


class SyncStatusView(APIView):
    def get(self, request):
        logs = SyncLog.objects.for_user(request.user)[:10]
        items = PlaidItem.objects.for_user(request.user)
        count = Transaction.objects.for_user(request.user).filter(removed_at__isnull=True).count()
        return Response(
            {
                "logs": s.SyncLogSerializer(logs, many=True).data,
                "items": [
                    {
                        "id": str(i.id),
                        "institution_name": i.institution_name,
                        "last_sync_at": i.last_sync_at,
                        "connection_status": i.connection_status,
                        "initial_sync_done": i.initial_sync_done,
                        "error_message": i.error_message,
                    }
                    for i in items
                ],
                "transaction_count": count,
            }
        )
