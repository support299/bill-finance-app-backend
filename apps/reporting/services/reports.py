"""Cash-flow report and dashboard aggregates (port of src/lib/reports.functions.ts)."""

from datetime import date, timedelta

from apps.banking.models import BankAccount, PlaidItem
from apps.banking.serializers import BankAccountSerializer
from apps.ledger.models import Transaction


def _bucket(d: date, granularity: str) -> str:
    if granularity == "day":
        return d.isoformat()
    if granularity == "month":
        return d.isoformat()[:7]
    return (d - timedelta(days=d.weekday())).isoformat()  # week starts Monday


def _row(t: Transaction) -> dict:
    c = t.user_category
    return {
        "transaction_date": t.transaction_date.isoformat(),
        "amount": float(t.amount),
        "merchant_name": t.merchant_name,
        "description": t.description,
        "plaid_account_id": t.plaid_account_id,
        "is_income": t.is_income,
        "is_expense": t.is_expense,
        "is_transfer": t.is_transfer,
        "currency": t.currency,
        "categories": {"id": str(c.id), "name": c.name, "type": c.type, "color": c.color} if c else None,
    }


def _accumulate(bucket: dict, name: str, color, value: float):
    entry = bucket.setdefault(name, {"name": name, "color": color, "value": 0.0})
    entry["value"] += value


def report_data(user, *, date_from, date_to, account_ids=None, include_transfers=False, granularity="month") -> dict:
    qs = (
        Transaction.objects.for_user(user)
        .filter(removed_at__isnull=True, is_hidden=False, transaction_date__gte=date_from, transaction_date__lte=date_to)
        .select_related("user_category")
        .order_by("transaction_date")
    )
    if account_ids:
        qs = qs.filter(plaid_account_id__in=account_ids)
    rows = [_row(t) for t in qs[:20000]]
    rows = [r for r in rows if include_transfers or not r["is_transfer"]]

    total_income = total_expenses = transfer_volume = 0.0
    series: dict = {}
    expense_by_cat: dict = {}
    income_by_cat: dict = {}
    merchants: dict = {}
    largest_expense = largest_income = None

    for r in rows:
        amount = r["amount"]
        key = _bucket(date.fromisoformat(r["transaction_date"]), granularity)
        entry = series.setdefault(key, {"period": key, "income": 0.0, "expenses": 0.0, "net": 0.0})
        cat = r["categories"]
        cat_name = cat["name"] if cat else "Uncategorized"
        cat_color = cat["color"] if cat else None
        if r["is_transfer"]:
            transfer_volume += abs(amount)
        elif r["is_income"]:
            v = abs(amount)
            total_income += v
            entry["income"] += v
            _accumulate(income_by_cat, cat_name, cat_color, v)
            if largest_income is None or abs(largest_income["amount"]) < v:
                largest_income = r
        elif r["is_expense"]:
            v = abs(amount)
            total_expenses += v
            entry["expenses"] += v
            _accumulate(expense_by_cat, cat_name, cat_color, v)
            m = r["merchant_name"] or r["description"] or "Unknown"
            merchants[m] = merchants.get(m, 0.0) + v
            if largest_expense is None or abs(largest_expense["amount"]) < v:
                largest_expense = r
        entry["net"] = entry["income"] - entry["expenses"]

    month_count = max(len({r["transaction_date"][:7] for r in rows}), 1)
    sorted_expense = sorted(expense_by_cat.values(), key=lambda x: -x["value"])
    sorted_income = sorted(income_by_cat.values(), key=lambda x: -x["value"])
    return {
        "totals": {
            "total_income": total_income,
            "total_expenses": total_expenses,
            "net_cash_flow": total_income - total_expenses,
            "average_monthly_income": total_income / month_count,
            "average_monthly_expenses": total_expenses / month_count,
            "transfer_volume": transfer_volume,
            "transaction_count": len(rows),
            "month_count": month_count,
        },
        "series": sorted(series.values(), key=lambda x: x["period"]),
        "expense_by_category": sorted_expense,
        "income_by_category": sorted_income,
        "top_expense_category": sorted_expense[0] if sorted_expense else None,
        "top_income_category": sorted_income[0] if sorted_income else None,
        "top_merchants": [{"name": n, "value": v} for n, v in sorted(merchants.items(), key=lambda kv: -kv[1])[:10]],
        "largest_expense": largest_expense,
        "largest_income": largest_income,
        "largest_transactions": sorted((r for r in rows if r["is_expense"]), key=lambda r: -abs(r["amount"]))[:10],
    }


def dashboard_data(user, account_ids=None) -> dict:
    accounts = BankAccount.objects.for_user(user)
    if account_ids:
        accounts = accounts.filter(plaid_account_id__in=account_ids)
    accounts = list(accounts)
    balance = 0.0
    for a in accounts:
        bal = float(a.current_balance or 0)
        balance += -bal if a.account_type in ("credit", "loan") else bal
    items = PlaidItem.objects.for_user(user).values("id", "institution_name", "connection_status", "error_message", "last_sync_at")
    return {
        "accounts": BankAccountSerializer(accounts, many=True).data,
        "items": list(items),
        "current_balance": balance,
    }
