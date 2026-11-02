"""Reconnecting a bank whose history was imported from the old app.

The imported connection is a token-less placeholder (connection_status "imported"). When the same bank is connected again, Plaid
re-sends its recent history as brand-new rows. For each account that matches (same type and last digits) the imported
row is kept, because it carries the user's categories, ledgers and project tags, and it takes over the fresh Plaid ids; the freshly
synced twin is dropped. Rows only the new sync has stay as genuinely new transactions.
"""

from collections import defaultdict

from django.db import transaction as db_transaction

from apps.ledger.models import Transaction

from ..models import BankAccount, PlaidItem

IMPORTED = "imported"


def _key(t) -> tuple:
    from decimal import Decimal

    desc = " ".join((t.original_description or t.description or t.merchant_name or "").lower().split())
    return (t.transaction_date, Decimal(t.amount).quantize(Decimal("0.01")), desc)


def adopt_imported(new_item: PlaidItem) -> dict:
    placeholders = PlaidItem.objects.filter(user_id=new_item.user_id, connection_status=IMPORTED).exclude(pk=new_item.pk)
    stats = {"merged": 0, "carried_over": 0, "accounts": 0}
    for ph in placeholders:
        same_bank = (ph.institution_id and ph.institution_id == new_item.institution_id) or (
            ph.institution_name and ph.institution_name == new_item.institution_name
        )
        if not same_bank:
            continue
        for old_acc in list(BankAccount.objects.filter(plaid_item=ph)):
            new_acc = BankAccount.objects.filter(plaid_item=new_item, mask=old_acc.mask, account_type=old_acc.account_type).first()
            if new_acc is None:
                continue
            with db_transaction.atomic():
                old_rows = list(
                    Transaction.objects.filter(plaid_item=ph, plaid_account_id=old_acc.plaid_account_id, removed_at__isnull=True)
                )
                fresh: dict = defaultdict(list)
                for t in Transaction.objects.filter(plaid_item=new_item, plaid_account_id=new_acc.plaid_account_id).order_by("created_at"):
                    fresh[_key(t)].append(t)
                for old in old_rows:
                    twins = fresh.get(_key(old))
                    update = {"plaid_item": new_item, "plaid_account_id": new_acc.plaid_account_id}
                    if twins:
                        twin = twins.pop(0)
                        update.update(plaid_transaction_id=twin.plaid_transaction_id, pending=twin.pending)
                        twin.delete()  # frees the unique Plaid id before the imported row takes it over
                        stats["merged"] += 1
                    else:
                        stats["carried_over"] += 1
                    Transaction.objects.filter(pk=old.pk).update(**update)
                if not Transaction.objects.filter(plaid_item=ph, plaid_account_id=old_acc.plaid_account_id).exists():
                    old_acc.delete()
                stats["accounts"] += 1
        if not BankAccount.objects.filter(plaid_item=ph).exists() and not Transaction.objects.filter(plaid_item=ph).exists():
            ph.delete()  # nothing left to keep: the placeholder has been fully taken over
    return stats
