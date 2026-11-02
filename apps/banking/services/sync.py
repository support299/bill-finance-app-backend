"""Plaid /transactions/sync pipeline (port of src/lib/sync.server.ts).

Idempotent: transactions are upserted on plaid_transaction_id and the cursor is only advanced after
each page is persisted successfully.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.banking.models import BankAccount, PlaidItem, SyncLog
from apps.core.crypto import decrypt_secret
from apps.core.services import notify
from apps.ledger.models import Transaction
from apps.ledger.services.categorizer import CategorizerInput, categorize_transaction, resolve_ledger_id
from apps.ledger.services.transactions import load_categories_and_rules

from . import plaid_client as plaid
from .plaid_client import PlaidError, friendly_plaid_error

logger = logging.getLogger(__name__)


@dataclass
class SyncOutcome:
    added: int = 0
    modified: int = 0
    removed: int = 0
    status: str = "success"
    message: str | None = None

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}


def refresh_accounts(item: PlaidItem, access_token: str) -> int:
    accounts = plaid.accounts_get(access_token)["accounts"]
    for acc in accounts:
        bal = acc.get("balances") or {}
        BankAccount.objects.update_or_create(
            plaid_account_id=acc["account_id"],
            defaults={
                "user_id": item.user_id,
                "plaid_item": item,
                "institution_id": item.institution_id,
                "account_name": acc.get("name"),
                "official_name": acc.get("official_name"),
                "mask": acc.get("mask"),
                "account_type": acc.get("type"),
                "account_subtype": acc.get("subtype"),
                "current_balance": bal.get("current"),
                "available_balance": bal.get("available"),
                "iso_currency_code": bal.get("iso_currency_code") or "USD",
                "last_balance_update": timezone.now(),
            },
        )
    return len(accounts)


def _to_fields(tx: dict, item: PlaidItem, account_types: dict, categories, rules) -> dict:
    pfc = tx.get("personal_finance_category") or {}
    loc = tx.get("location") or {}
    inp = CategorizerInput(
        merchant_name=tx.get("merchant_name"),
        description=tx.get("name"),
        original_description=tx.get("original_description"),
        plaid_primary=pfc.get("primary"),
        plaid_detailed=pfc.get("detailed"),
        amount=float(tx["amount"]),
        payment_channel=tx.get("payment_channel"),
        transaction_type=tx.get("transaction_type"),
        account_type=account_types.get(tx["account_id"]),
    )
    result = categorize_transaction(inp, categories, rules)
    fields = {
        "user_id": item.user_id,
        "plaid_item": item,
        "plaid_account_id": tx["account_id"],
        "merchant_name": tx.get("merchant_name"),
        "original_description": tx.get("original_description") or tx.get("name"),
        "description": tx.get("name"),
        # Stored in "account view": negative = money out, positive = money in.
        "amount": -Decimal(str(tx["amount"])),
        "currency": tx.get("iso_currency_code") or tx.get("unofficial_currency_code") or "USD",
        "transaction_date": tx["date"],
        "authorized_date": tx.get("authorized_date"),
        "pending": bool(tx.get("pending")),
        "status": "pending" if tx.get("pending") else "posted",
        "payment_channel": tx.get("payment_channel"),
        "transaction_type": tx.get("transaction_type"),
        "merchant_entity_id": tx.get("merchant_entity_id"),
        "merchant_logo_url": tx.get("logo_url"),
        "website": tx.get("website"),
        "city": loc.get("city"),
        "region": loc.get("region"),
        "postal_code": loc.get("postal_code"),
        "country": loc.get("country"),
        "plaid_personal_finance_primary_category": pfc.get("primary"),
        "plaid_personal_finance_detailed_category": pfc.get("detailed"),
        "plaid_category_confidence": pfc.get("confidence_level"),
        "user_category_id": result.category_id,
        "category_source": result.source,
        "categorization_confidence": result.confidence,
        "transaction_direction": result.direction,
        "is_income": result.is_income,
        "is_expense": result.is_expense,
        "is_transfer": result.is_transfer,
        "gaap_type": result.type,
        "removed_at": None,
    }
    ledger_id = resolve_ledger_id(inp, rules)
    if ledger_id:  # never wipe a ledger the user assigned by hand
        fields["ledger_id"] = ledger_id
    return fields


def sync_item(item_id, trigger: str = "manual") -> SyncOutcome:
    item = PlaidItem.objects.filter(pk=item_id).first()
    if not item:
        return SyncOutcome(status="error", message="Connection not found.")

    if not item.access_token_encrypted:  # history imported from the old app: reconnect the bank to resume syncing
        return SyncOutcome(status="skipped", message="Reconnect this bank to resume syncing.")
    access_token = decrypt_secret(item.access_token_encrypted)
    log = SyncLog.objects.create(user_id=item.user_id, plaid_item=item, status="running", trigger_source=trigger)
    added = modified = removed = 0

    try:
        refresh_accounts(item, access_token)
        account_types = dict(BankAccount.objects.filter(plaid_item=item).values_list("plaid_account_id", "account_type"))
        categories, rules = load_categories_and_rules(item.user)

        cursor = item.sync_cursor
        has_more, guard = True, 0
        while has_more and guard < 100:
            guard += 1
            page = plaid.transactions_sync(access_token, cursor)

            with db_transaction.atomic():  # cursor only advances if the page persists
                # Rows in a completed reconciliation are frozen: the verified figures must not shift under the user.
                changed_ids = [t["transaction_id"] for t in [*page["added"], *page["modified"], *page["removed"]]]
                frozen = set(
                    Transaction.objects.filter(plaid_transaction_id__in=changed_ids, reconciliation__status="completed").values_list(
                        "plaid_transaction_id", flat=True
                    )
                )
                for tx in [*page["added"], *page["modified"]]:
                    if tx["transaction_id"] in frozen:
                        continue
                    fields = _to_fields(tx, item, account_types, categories, rules)
                    Transaction.objects.update_or_create(plaid_transaction_id=tx["transaction_id"], defaults={**fields, "source": "plaid"})
                if page["removed"]:
                    Transaction.objects.filter(
                        user=item.user,
                        plaid_transaction_id__in=[r["transaction_id"] for r in page["removed"] if r["transaction_id"] not in frozen],
                    ).update(removed_at=timezone.now())

                added += len(page["added"])
                modified += len(page["modified"])
                removed += len(page["removed"])
                cursor, has_more = page["next_cursor"], page["has_more"]
                PlaidItem.objects.filter(pk=item.pk).update(sync_cursor=cursor, last_sync_at=timezone.now())

        was_initial = item.initial_sync_done
        PlaidItem.objects.filter(pk=item.pk).update(
            connection_status="good",
            error_code=None,
            error_message=None,
            initial_sync_done=True,
            last_sync_at=timezone.now(),
        )
        SyncLog.objects.filter(pk=log.pk).update(
            status="success",
            sync_completed_at=timezone.now(),
            transactions_added=added,
            transactions_modified=modified,
            transactions_removed=removed,
        )
        if added > 0:
            notify(
                item.user,
                "New transactions synced" if was_initial else "Initial transaction import complete",
                f"{added} transaction{'' if added == 1 else 's'} imported from {item.institution_name or 'your bank'}.",
                "success",
            )
        return SyncOutcome(added, modified, removed, "success")
    except Exception as exc:  # noqa: BLE001 - failures are recorded on the item and log
        err = exc if isinstance(exc, PlaidError) else None
        if not err:
            logger.exception("Sync failed for item %s", item.pk)
        message = friendly_plaid_error(err.error_code) if err else "Synchronization failed. Please try again."
        PlaidItem.objects.filter(pk=item.pk).update(
            connection_status="requires_reauth" if err and err.error_code == "ITEM_LOGIN_REQUIRED" else "error",
            error_code=(err.error_code if err else None) or "SYNC_ERROR",
            error_message=message,
        )
        SyncLog.objects.filter(pk=log.pk).update(status="error", sync_completed_at=timezone.now(), error_message=message)
        notify(item.user, "Bank connection needs attention", message, "error")
        return SyncOutcome(added, modified, removed, "error", message)


def refresh_item_status(item_id) -> None:
    item = PlaidItem.objects.filter(pk=item_id).first()
    if not item:
        return
    try:
        remote = plaid.item_get(decrypt_secret(item.access_token_encrypted))["item"]
    except Exception:  # noqa: BLE001 - status refresh is best-effort
        return
    err = remote.get("error")
    if err:
        code = err.get("error_code")
        PlaidItem.objects.filter(pk=item.pk).update(
            connection_status="requires_reauth" if code == "ITEM_LOGIN_REQUIRED" else "error",
            error_code=code,
            error_message=friendly_plaid_error(code),
        )
    else:
        PlaidItem.objects.filter(pk=item.pk).update(connection_status="good", error_code=None, error_message=None)
