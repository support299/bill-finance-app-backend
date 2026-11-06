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
from .models import BankAccount, PlaidItem, Reconciliation, SyncLog
from .services import plaid_client as plaid
from .services import csv_import, reconcile
from .services.adopt import adopt_imported
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
            # An imported (token-less) connection has nothing to update: it is reconnected with a fresh Link.
            access_token = decrypt_secret(item.access_token_encrypted) if item.access_token_encrypted else None
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
        if not existing and fields["institution_id"]:
            # A fresh connection to a bank the user already trimmed: keep ignoring the history they deleted.
            sibling = (
                PlaidItem.objects.filter(user=request.user, institution_id=fields["institution_id"], history_from__isnull=False)
                .order_by("-history_from")
                .first()
            )
            if sibling:
                fields["history_from"] = sibling.history_from
        item, _ = PlaidItem.objects.update_or_create(item_id=item_id, defaults=fields)
        audit(request.user, "bank_connected", "plaid_item", str(item.id), {"institution": item.institution_name})
        notify(request.user, "Bank connected", f"{item.institution_name} is connected. Importing transactions…", "success")
        outcome = sync_item(item.id, trigger="connect")
        adopt_imported(item)  # merge any history imported from the old app into this fresh connection
        return Response({"item_id": str(item.id), **outcome.as_dict()})


class CsvImportView(APIView):
    """Fallback for banks Plaid cannot reach: upload a CSV downloaded from the bank. dry_run previews without saving."""

    def post(self, request):
        ser = s.CsvImportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        try:
            parsed = csv_import.parse_csv(d["csv_text"].encode("utf-8"), invert_sign=d["invert_sign"])
            if d["dry_run"]:
                account = BankAccount(id=d.get("account_id") or "00000000-0000-0000-0000-000000000000")
                if d.get("account_id"):
                    account = csv_import.get_or_create_account(request.user, account_id=d["account_id"])
                return Response(csv_import.import_rows(request.user, account, parsed, commit=False))
            account = csv_import.get_or_create_account(
                request.user,
                account_id=d.get("account_id"),
                institution_name=d.get("institution_name", ""),
                account_name=d.get("account_name", ""),
                account_type=d["account_type"],
                mask=d.get("mask", ""),
            )
        except csv_import.CsvImportError as exc:
            raise ValidationError({"detail": str(exc)})
        summary = csv_import.import_rows(request.user, account, parsed)
        audit(request.user, "csv_imported", "bank_account", str(account.id), {k: summary[k] for k in ("added", "duplicates", "skipped")})
        if summary["added"]:
            notify(
                request.user,
                "CSV import complete",
                f"{summary['added']} transaction{'' if summary['added'] == 1 else 's'} imported into {account.account_name}.",
                "success",
            )
        return Response({"account_id": str(account.id), **summary}, status=status.HTTP_201_CREATED if summary["added"] else status.HTTP_200_OK)


class SyncView(APIView):
    def post(self, request):
        ser = s.SyncRequestSerializer(data=request.data or {})
        ser.is_valid(raise_exception=True)
        items = PlaidItem.objects.for_user(request.user).exclude(access_token_encrypted="")
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
        if not item.access_token_encrypted:
            raise ValidationError({"detail": "Reconnect this bank first: its history was imported without a live connection."})
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
    def patch(self, request, item_id):
        """Change where a bank's history starts: transactions dated before `history_from` are ignored by the sync."""
        item = get_object_or_404(PlaidItem.objects.for_user(request.user), pk=item_id)
        ser = s.PlaidItemUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        item.history_from = ser.validated_data["history_from"]
        item.save(update_fields=["history_from"])
        audit(request.user, "bank_history_from_changed", "plaid_item", str(item.id), {"history_from": str(item.history_from)})
        return Response(s.PlaidItemSerializer(item).data)

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


# -------------------------------------------------------------- reconciliation
def _rec_or_404(request, rec_id) -> Reconciliation:
    return get_object_or_404(Reconciliation.objects.for_user(request.user).select_related("bank_account"), pk=rec_id)


def _rec_payload(rec: Reconciliation) -> dict:
    return {"reconciliation": reconcile.summary(rec), "transactions": reconcile.rows(rec)}


class ReconciliationListView(APIView):
    def get(self, request):
        qs = Reconciliation.objects.for_user(request.user).select_related("bank_account")
        if request.query_params.get("account_id"):
            qs = qs.filter(bank_account_id=request.query_params["account_id"])
        return Response({"reconciliations": [reconcile.summary(r) for r in qs]})

    def post(self, request):
        ser = s.ReconciliationCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        account = get_object_or_404(BankAccount.objects.for_user(request.user), pk=d["account_id"])
        if Reconciliation.objects.filter(bank_account=account, status="in_progress").exists():
            raise ValidationError({"detail": "Finish or delete the reconciliation already in progress for this account."})
        previous = reconcile.last_completed(account, request.user.id)
        if previous and d["statement_date"] <= previous.statement_date:
            raise ValidationError({"statement_date": f"This account is already reconciled through {previous.statement_date}."})
        # Each statement starts where the last verified one ended.
        beginning = previous.ending_balance if previous else d.get("beginning_balance", 0)
        rec = Reconciliation.objects.create(
            user=request.user,
            bank_account=account,
            statement_date=d["statement_date"],
            ending_balance=d["ending_balance"],
            beginning_balance=beginning,
        )
        return Response(_rec_payload(rec), status=status.HTTP_201_CREATED)


class ReconciliationDetailView(APIView):
    def get(self, request, rec_id):
        return Response(_rec_payload(_rec_or_404(request, rec_id)))

    def patch(self, request, rec_id):
        rec = _rec_or_404(request, rec_id)
        if rec.status != "in_progress":
            raise ValidationError({"detail": "Reopen this reconciliation before changing it."})
        ser = s.ReconciliationUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        previous = reconcile.last_completed(rec.bank_account, request.user.id)
        if previous and d.get("statement_date") and d["statement_date"] <= previous.statement_date:
            raise ValidationError({"statement_date": f"This account is already reconciled through {previous.statement_date}."})
        if previous:
            d.pop("beginning_balance", None)
        for k, v in d.items():
            setattr(rec, k, v)
        rec.save()
        if "statement_date" in d:  # transactions after the new date can no longer be part of it
            rec.transactions.filter(transaction_date__gt=rec.statement_date).update(reconciliation=None)
        return Response(_rec_payload(rec))

    def delete(self, request, rec_id):
        rec = _rec_or_404(request, rec_id)
        if rec.status != "in_progress":
            raise ValidationError({"detail": "A completed reconciliation must be reopened before it can be deleted."})
        rec.delete()  # ticked transactions are released (FK is SET_NULL)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReconciliationClearView(APIView):
    def post(self, request, rec_id):
        rec = _rec_or_404(request, rec_id)
        if rec.status != "in_progress":
            raise ValidationError({"detail": "This reconciliation is completed."})
        ser = s.ReconciliationClearSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        pool = reconcile.candidates(rec).filter(id__in=d["ids"])
        pool.update(reconciliation=rec if d["cleared"] else None)
        return Response(_rec_payload(rec))


class ReconciliationFinishView(APIView):
    def post(self, request, rec_id):
        from django.utils import timezone

        rec = _rec_or_404(request, rec_id)
        if rec.status != "in_progress":
            raise ValidationError({"detail": "This reconciliation is already completed."})
        info = reconcile.summary(rec)
        if not info["balanced"]:
            raise ValidationError({"detail": f"The difference must be 0.00 to finish (currently {info['difference']})."})
        rec.status, rec.completed_at = "completed", timezone.now()
        rec.save(update_fields=["status", "completed_at", "updated_at"])
        audit(request.user, "reconciliation_completed", "reconciliation", str(rec.id), {"statement_date": str(rec.statement_date)})
        return Response(_rec_payload(rec))


class ReconciliationReopenView(APIView):
    def post(self, request, rec_id):
        rec = _rec_or_404(request, rec_id)
        latest = reconcile.last_completed(rec.bank_account, request.user.id)
        if rec.status != "completed" or not latest or latest.id != rec.id:
            raise ValidationError({"detail": "Only the most recent completed reconciliation of an account can be reopened."})
        if Reconciliation.objects.filter(bank_account=rec.bank_account, status="in_progress").exists():
            raise ValidationError({"detail": "Finish or delete the reconciliation already in progress first."})
        rec.status, rec.completed_at = "in_progress", None
        rec.save(update_fields=["status", "completed_at", "updated_at"])
        audit(request.user, "reconciliation_reopened", "reconciliation", str(rec.id))
        return Response(_rec_payload(rec))
