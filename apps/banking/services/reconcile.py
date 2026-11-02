"""Bank / credit-card reconciliation.

Transactions are stored in "account view" (negative = money out). Statements are not: a credit-card or loan statement shows
what you *owe*, which goes up with charges. So every amount is converted to statement terms with ``statement_sign``.
"""

from decimal import Decimal

from django.db.models import Q

from apps.ledger.models import Transaction

from ..models import BankAccount, Reconciliation

TOLERANCE = Decimal("0.005")


def statement_sign(account: BankAccount) -> int:
    return -1 if account.account_type in ("credit", "loan") else 1


def candidates(rec: Reconciliation):
    """Posted activity on the account up to the statement date that is free or already ticked in this reconciliation."""
    return (
        Transaction.objects.filter(
            user_id=rec.user_id,
            plaid_account_id=rec.bank_account.plaid_account_id,
            removed_at__isnull=True,
            pending=False,
            transaction_date__lte=rec.statement_date,
        )
        .exclude(status="void")
        .filter(Q(reconciliation__isnull=True) | Q(reconciliation=rec))
        .order_by("transaction_date", "created_at")
    )


def summary(rec: Reconciliation) -> dict:
    sign = statement_sign(rec.bank_account)
    deltas = [sign * t for t in rec.transactions.filter(removed_at__isnull=True).values_list("amount", flat=True)]
    additions = sum((d for d in deltas if d > 0), Decimal(0))
    subtractions = -sum((d for d in deltas if d < 0), Decimal(0))
    cleared_balance = rec.beginning_balance + additions - subtractions
    difference = rec.ending_balance - cleared_balance
    return {
        "id": str(rec.id),
        "bank_account_id": str(rec.bank_account_id),
        "account_name": rec.bank_account.account_name,
        "mask": rec.bank_account.mask,
        "account_type": rec.bank_account.account_type,
        "statement_date": rec.statement_date,
        "beginning_balance": rec.beginning_balance,
        "ending_balance": rec.ending_balance,
        "status": rec.status,
        "completed_at": rec.completed_at,
        "cleared_count": len(deltas),
        "additions": additions,
        "subtractions": subtractions,
        "cleared_balance": cleared_balance,
        "difference": difference,
        "balanced": abs(difference) < TOLERANCE,
    }


def rows(rec: Reconciliation) -> list[dict]:
    sign = statement_sign(rec.bank_account)
    source = rec.transactions.filter(removed_at__isnull=True).order_by("transaction_date", "created_at")
    if rec.status == "in_progress":
        source = candidates(rec)
    return [
        {
            "id": str(t.id),
            "date": t.transaction_date,
            "description": t.merchant_name or t.description or "—",
            "amount": t.amount,
            "delta": sign * t.amount,
            "cleared": t.reconciliation_id == rec.id,
        }
        for t in source
    ]


def last_completed(account: BankAccount, user_id):
    return Reconciliation.objects.filter(bank_account=account, user_id=user_id, status="completed").order_by("-statement_date").first()


def is_locked(tx: Transaction) -> bool:
    return bool(tx.reconciliation_id and tx.reconciliation.status == "completed")
