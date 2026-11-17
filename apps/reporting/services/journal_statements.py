"""Profit & loss, cash flow, trial balance and general ledger read from the double-entry journal, in the shapes the report pages already use.

Because every part of a split transaction is its own journal line, these reports see splits (by category and by project) with no
special handling. Balance sheet lives in journal_balance.py.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.ledger.models import Account, Category, JournalLine
from apps.ledger.services.journal import CLEARING, GHL_CLEARING, MANUAL_CASH, PAYABLE, RECEIVABLE

BUCKETS = ("revenue", "cogs", "operating_expense", "other_income", "other_expense")
INCOME_BUCKETS = ("revenue", "other_income")
UNCATEGORIZED_ACTIVITY = "Uncategorized activity"
CLASS_FOR_KIND = {
    "asset": "current_asset",
    "liability": "current_liability",
    "equity": "equity",
    "income": "income",
    "expense": "expense",
}


def _f(v) -> float:
    return float(round(Decimal(v or 0), 2))


def profit_and_loss(user, *, date_from: date, date_to: date, project_id=None, pl_sort: str = "chart", positions: dict | None = None):
    """(profit_and_loss, total_income, total_expenses) with the same shape and line grouping as the transaction-based report."""
    from .accounting import _category_lines

    positions = positions or {}
    qs = JournalLine.objects.filter(
        entry__user=user,
        entry__kind="auto",
        entry__date__gte=date_from,
        entry__date__lte=date_to,
        account__kind__in=("income", "expense"),
    )
    if project_id:
        qs = qs.filter(project_id=project_id)
    rows = qs.values("account_id", "account__kind", "account__category_id", "gaap_type").annotate(d=Sum("debit"), c=Sum("credit"))

    cat_ids = {r["account__category_id"] for r in rows if r["account__category_id"]}
    cats = {c.id: c for c in Category.objects.filter(id__in=cat_ids).select_related("parent_category")}
    pl: dict = {b: {} for b in BUCKETS}
    for r in rows:
        bucket = r["gaap_type"] if r["gaap_type"] in BUCKETS else ("revenue" if r["account__kind"] == "income" else "operating_expense")
        value = _f((r["c"] or 0) - (r["d"] or 0)) if bucket in INCOME_BUCKETS else _f((r["d"] or 0) - (r["c"] or 0))
        cid = r["account__category_id"]
        c = cats.get(cid)
        parent = c.parent_category if c else None
        line = pl[bucket].setdefault(
            cid,
            {
                "name": c.name if c else "Uncategorized",
                "value": 0.0,
                "category_id": str(cid) if cid else None,
                "sort": positions.get(c.id, c.sort_order) if c else 0,
                "parent_id": str(parent.id) if parent else None,
                "parent_name": parent.name if parent else None,
                "parent_sort": positions.get(parent.id, parent.sort_order) if parent else 0,
            },
        )
        line["value"] += value

    tot = lambda m: sum(x["value"] for x in m.values())  # noqa: E731
    revenue, cogs, opex = tot(pl["revenue"]), tot(pl["cogs"]), tot(pl["operating_expense"])
    other_income, other_expense = tot(pl["other_income"]), tot(pl["other_expense"])
    gross = revenue - cogs
    total_income, total_expenses = revenue + other_income, cogs + opex + other_expense
    return (
        {
            "revenue": _category_lines(pl["revenue"], pl_sort),
            "cogs": _category_lines(pl["cogs"], pl_sort),
            "operating_expenses": _category_lines(pl["operating_expense"], pl_sort),
            "other_income": _category_lines(pl["other_income"], pl_sort),
            "other_expenses": _category_lines(pl["other_expense"], pl_sort),
            "total_revenue": revenue,
            "total_cogs": cogs,
            "gross_profit": gross,
            "total_operating_expenses": opex,
            "operating_income": gross - opex,
            "total_other_income": other_income,
            "total_other_expenses": other_expense,
            "net_other": other_income - other_expense,
            "total_income": total_income,
            "total_expenses": total_expenses,
            "net_profit": total_income - total_expenses,
        },
        total_income,
        total_expenses,
    )


def trial_balance(user, *, date_to: date) -> dict:
    """Every account's balance up to the date, on its debit or credit side. Debits equal credits because every entry balances."""
    accounts = {a.id: a for a in Account.objects.filter(user=user)}
    sums = (
        JournalLine.objects.filter(entry__user=user, entry__date__lte=date_to)
        .values("account_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    rows = []
    for r in sums:
        net = Decimal(r["d"] or 0) - Decimal(r["c"] or 0)
        if not net:
            continue
        a = accounts[r["account_id"]]
        rows.append(
            {
                "id": str(a.id),
                "label": a.name,
                "account_class": CLASS_FOR_KIND[a.kind],
                "debit": _f(net) if net > 0 else 0.0,
                "credit": _f(-net) if net < 0 else 0.0,
                "_code": a.code,
            }
        )
    order = list(CLASS_FOR_KIND.values())
    rows.sort(key=lambda r: (order.index(r["account_class"]), r["_code"]))
    for r in rows:
        del r["_code"]
    debit, credit = round(sum(r["debit"] for r in rows), 2), round(sum(r["credit"] for r in rows), 2)
    return {
        "rows": rows,
        "total_debit": debit,
        "total_credit": credit,
        "difference": round(debit - credit, 2),
        "balanced": abs(debit - credit) < 0.005,
    }


def general_ledger(user, *, date_from: date, date_to: date) -> list:
    """One block per account with activity in the range: its lines in date order and a running balance that starts from the
    balance the account had before the range."""
    accounts = {a.id: a for a in Account.objects.filter(user=user)}
    before = {
        r["account_id"]: Decimal(r["d"] or 0) - Decimal(r["c"] or 0)
        for r in JournalLine.objects.filter(entry__user=user, entry__date__lt=date_from)
        .values("account_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    }
    lines = (
        JournalLine.objects.filter(entry__user=user, entry__date__gte=date_from, entry__date__lte=date_to)
        .select_related("entry", "entry__transaction")
        .order_by("entry__date", "entry__created_at", "id")
    )
    by_account: dict = defaultdict(list)
    for ln in lines:
        by_account[ln.account_id].append(ln)
    blocks = []
    for aid, items in by_account.items():
        a = accounts[aid]
        sign = 1 if a.kind in ("asset", "expense") else -1  # show each account on its own normal side
        running = sign * before.get(aid, Decimal(0))
        entries = []
        for ln in items:
            running += sign * (ln.debit - ln.credit)
            t = ln.entry.transaction
            entries.append(
                {
                    "id": str(t.id if t else ln.entry_id),
                    "date": ln.entry.date.isoformat(),
                    "description": ln.entry.memo or "—",
                    "debit": _f(ln.debit),
                    "credit": _f(ln.credit),
                    "balance": _f(running),
                    "status": (t.status if t else None) or "posted",
                    "accounting_status": t.accounting_status if t else None,
                }
            )
        blocks.append(
            {
                "id": str(a.id),
                "label": a.name,
                "account_class": CLASS_FOR_KIND[a.kind],
                "entries": entries,
                "closing": _f(running),
                "_code": a.code,
            }
        )
    blocks.sort(key=lambda b: b["_code"])
    for b in blocks:
        del b["_code"]
    return blocks


def cash_flow(user, *, date_from: date, date_to: date) -> dict:
    """Where cash came from and went, read from entries that touch a cash account (a bank or card account, manual cash, or money
    collected through GoHighLevel; hand-entered cash only once marked received or paid). Each such entry's other lines say what the cash was for:
    income and expense lines are operating, other assets investing, liabilities and equity financing. Transfers between your own
    accounts never appear because both of their sides are cash."""
    accounts = {a.id: a for a in Account.objects.filter(user=user)}
    system = {a.system_key: a for a in accounts.values() if a.system_key}
    cash_ids = {a.id for a in accounts.values() if a.plaid_account_id} | {
        system[k.key[1]].id for k in (MANUAL_CASH, GHL_CLEARING) if k.key[1] in system
    }
    manual_cash = system.get(MANUAL_CASH.key[1])
    skip = {system[CLEARING.key[1]].id} if CLEARING.key[1] in system else set()
    receivable_payable = {system[k.key[1]].id for k in (RECEIVABLE, PAYABLE) if k.key[1] in system}
    ghl = system.get(GHL_CLEARING.key[1])

    lines = JournalLine.objects.filter(entry__user=user, entry__kind="auto", entry__date__gte=date_from, entry__date__lte=date_to)
    by_entry: dict = defaultdict(list)
    for ln in lines.select_related("entry__transaction"):
        by_entry[ln.entry_id].append(ln)

    flows: dict = {"operating": defaultdict(float), "investing": defaultdict(float), "financing": defaultdict(float)}
    for items in by_entry.values():
        tx = items[0].entry.transaction
        # A hand-entered row only counts as cash once it is marked received or paid; until then it is just a record.
        manual_is_cash = tx is not None and tx.accounting_status in ("received", "paid")
        cash_here = cash_ids if manual_is_cash or manual_cash is None else cash_ids - {manual_cash.id}
        touched = [ln for ln in items if ln.account_id in cash_here]
        if not touched:
            continue
        via_ghl = ghl is not None and any(ln.account_id == ghl.id for ln in touched)
        for ln in items:
            if ln.account_id in cash_ids or ln.account_id in skip:
                continue
            a = accounts[ln.account_id]
            amount = float(ln.credit - ln.debit)  # a credit to the other side means cash came in
            if via_ghl:
                section, label = "operating", ("Collections from customers" if amount >= 0 else "Customer refunds and processing fees")
            elif a.kind in ("income", "expense") or a.id in receivable_payable:
                section, label = "operating", a.name
            elif a.kind == "asset":
                section, label = "investing", a.name
            else:
                section, label = "financing", a.name
            if label.startswith("Uncategorized"):
                label = UNCATEGORIZED_ACTIVITY
            flows[section][label] += amount

    pairs = lambda m: [{"name": k, "value": round(v, 2)} for k, v in sorted(m.items(), key=lambda kv: -abs(kv[1])) if round(v, 2)]  # noqa: E731
    operating, investing, financing = (round(sum(flows[k].values()), 2) for k in ("operating", "investing", "financing"))
    bank_ids = [a.id for a in accounts.values() if a.plaid_account_id and a.kind == "asset"]
    held = JournalLine.objects.filter(entry__user=user, entry__date__lte=date_to, account_id__in=bank_ids).aggregate(
        d=Sum("debit"), c=Sum("credit")
    )
    return {
        "operating_lines": pairs(flows["operating"]),
        "investing_lines": pairs(flows["investing"]),
        "financing_lines": pairs(flows["financing"]),
        "operating": operating,
        "investing": investing,
        "financing": financing,
        "net_change": round(operating + investing + financing, 2),
        "closing_cash": _f(Decimal(held["d"] or 0) - Decimal(held["c"] or 0)),
    }
