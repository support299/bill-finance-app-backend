"""US-GAAP financial statements (port of src/lib/accounting.functions.ts)."""

from datetime import date

from apps.banking.models import BankAccount
from apps.ledger.models import Ledger, Transaction
from apps.ledger.services.gaap import row_gaap_type


def _aging_bucket(days: int) -> str:
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def _is_posted(t) -> bool:
    return t.status != "void" and not t.pending and t.status != "pending"


def _sum(rows) -> float:
    return sum(r["balance"] for r in rows)


def _sorted_pairs(m: dict, key=lambda kv: -abs(kv[1])):
    return [{"name": k, "value": v} for k, v in sorted(m.items(), key=key)]


def _category_lines(m: dict):
    """P&L lines keyed by category id (None = uncategorized); category_id lets the UI open the matching transactions."""
    rows = sorted(m.values(), key=lambda r: -abs(r["value"]))
    return [{"name": r["name"], "value": r["value"], "category_id": r["category_id"]} for r in rows]


def accounting_reports(user, *, date_from: date, date_to: date, project_id=None) -> dict:
    all_tx = list(
        Transaction.objects.for_user(user)
        .filter(removed_at__isnull=True, is_hidden=False)
        .select_related("user_category")
        .order_by("transaction_date")[:20000]
    )
    ledgers = list(Ledger.objects.for_user(user))
    ledger_by_id = {led.id: led for led in ledgers}
    accounts = list(BankAccount.objects.for_user(user))

    in_range = [t for t in all_tx if date_from <= t.transaction_date <= date_to and _is_posted(t)]
    up_to = [t for t in all_tx if t.transaction_date <= date_to and _is_posted(t)]
    today = date.today()
    # Open receivables/payables stay outstanding even when dated after the report date.
    open_items = [t for t in all_tx if _is_posted(t) and (t.transaction_date <= date_to or date_to >= today)]
    statement_tx = [t for t in up_to if t.accounting_status not in ("received", "paid")]

    def cat_name(t):
        return t.user_category.name if t.user_category else "Uncategorized"

    # ---- Profit & loss
    def build_pl(rows):
        pl = {k: {} for k in ("revenue", "cogs", "operating_expense", "other_income", "other_expense")}
        for t in rows:
            g = row_gaap_type(t)
            bucket = pl.get(g)
            if bucket is None:
                continue
            raw = float(t.amount)
            v = raw if g in ("revenue", "other_income") else -raw
            key = t.user_category_id
            line = bucket.setdefault(key, {"name": cat_name(t), "value": 0.0, "category_id": str(key) if key else None})
            line["value"] += v
        tot = lambda m: sum(r["value"] for r in m.values())  # noqa: E731
        total_revenue, total_cogs, total_opex = tot(pl["revenue"]), tot(pl["cogs"]), tot(pl["operating_expense"])
        total_other_income, total_other_expense = tot(pl["other_income"]), tot(pl["other_expense"])
        gross_profit = total_revenue - total_cogs
        total_income = total_revenue + total_other_income
        total_expenses = total_cogs + total_opex + total_other_expense
        profit_and_loss = {
            "revenue": _category_lines(pl["revenue"]),
            "cogs": _category_lines(pl["cogs"]),
            "operating_expenses": _category_lines(pl["operating_expense"]),
            "other_income": _category_lines(pl["other_income"]),
            "other_expenses": _category_lines(pl["other_expense"]),
            "total_revenue": total_revenue,
            "total_cogs": total_cogs,
            "gross_profit": gross_profit,
            "total_operating_expenses": total_opex,
            "operating_income": gross_profit - total_opex,
            "total_other_income": total_other_income,
            "total_other_expenses": total_other_expense,
            "net_other": total_other_income - total_other_expense,
            "total_income": total_income,
            "total_expenses": total_expenses,
            "net_profit": total_income - total_expenses,
        }
        return profit_and_loss, total_income, total_expenses

    profit_and_loss, total_income, total_expenses = build_pl(in_range)
    if project_id:  # the P&L tab can be narrowed to one project; retained earnings below stay company-wide
        profit_and_loss, _, _ = build_pl([t for t in in_range if str(t.project_id) == str(project_id)])

    # ---- Ledger balances
    ledger_balance: dict = {}
    for t in statement_tx:
        if t.ledger_id:
            ledger_balance[t.ledger_id] = ledger_balance.get(t.ledger_id, 0.0) + float(t.amount)

    def by_class(cls):
        rows = [
            {"id": str(led.id), "label": led.label, "balance": ledger_balance.get(led.id, 0.0)}
            for led in ledgers
            if led.account_class == cls
        ]
        return sorted((r for r in rows if r["balance"] != 0), key=lambda r: -abs(r["balance"]))

    # ---- Balance sheet
    cash_accounts = [
        {
            "id": a.plaid_account_id,
            "label": f"{a.account_name or 'Account'}{f' ••{a.mask}' if a.mask else ''}",
            "balance": float(a.current_balance or 0),
            "type": a.account_type,
        }
        for a in accounts
    ]
    lite = lambda a: {"id": a["id"], "label": a["label"], "balance": a["balance"]}  # noqa: E731
    cash_assets = [lite(a) for a in cash_accounts if a["type"] not in ("credit", "loan")]
    card_lines = [{**lite(a), "balance": abs(a["balance"])} for a in cash_accounts if a["type"] == "credit"]
    loan_lines = [{**lite(a), "balance": abs(a["balance"])} for a in cash_accounts if a["type"] == "loan"]
    absrows = lambda rows: [{**r, "balance": abs(r["balance"])} for r in rows]  # noqa: E731

    current_assets = [*cash_assets, *by_class("current_asset")]
    noncurrent_assets = by_class("noncurrent_asset")
    current_liabilities = [*card_lines, *absrows(by_class("current_liability"))]
    noncurrent_liabilities = [*loan_lines, *absrows(by_class("noncurrent_liability"))]
    equity_accounts = by_class("equity")
    retained = total_income - total_expenses
    total_assets = _sum(current_assets) + _sum(noncurrent_assets)
    total_liabilities = _sum(current_liabilities) + _sum(noncurrent_liabilities)
    total_equity = _sum(equity_accounts) + retained
    balance_sheet = {
        "current_assets": current_assets,
        "noncurrent_assets": noncurrent_assets,
        "current_liabilities": current_liabilities,
        "noncurrent_liabilities": noncurrent_liabilities,
        "equity": [*equity_accounts, {"id": "retained", "label": "Retained earnings", "balance": retained}],
        "total_current_assets": _sum(current_assets),
        "total_noncurrent_assets": _sum(noncurrent_assets),
        "total_current_liabilities": _sum(current_liabilities),
        "total_noncurrent_liabilities": _sum(noncurrent_liabilities),
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
        "unbalanced": total_assets - (total_liabilities + total_equity),
    }

    # ---- Cash flow
    flows = {"operating": {}, "investing": {}, "financing": {}}
    for t in in_range:
        is_cash = (t.source not in ("manual", "ghl")) or t.accounting_status in ("received", "paid")
        if not is_cash or t.is_transfer:
            continue
        amt = float(t.amount)
        cls = ledger_by_id[t.ledger_id].account_class if t.ledger_id in ledger_by_id else None
        g = row_gaap_type(t)
        if t.source == "ghl":
            section = "operating"
        elif cls == "noncurrent_asset" or g == "asset":
            section = "investing"
        elif cls in ("noncurrent_liability", "equity") or g in ("liability", "equity"):
            section = "financing"
        else:
            section = "operating"
        if t.source == "ghl":
            label = "Collections from customers" if amt >= 0 else "Customer refunds and processing fees"
        elif t.user_category:
            label = t.user_category.name
        elif t.ledger_id in ledger_by_id:
            label = ledger_by_id[t.ledger_id].label
        else:
            label = "Uncategorized activity"
        flows[section][label] = flows[section].get(label, 0.0) + amt
    operating, investing, financing = (sum(flows[k].values()) for k in ("operating", "investing", "financing"))
    cash_flow = {
        "operating_lines": _sorted_pairs(flows["operating"]),
        "investing_lines": _sorted_pairs(flows["investing"]),
        "financing_lines": _sorted_pairs(flows["financing"]),
        "operating": operating,
        "investing": investing,
        "financing": financing,
        "net_change": operating + investing + financing,
        "closing_cash": sum(a["balance"] for a in cash_assets),
    }

    # ---- General ledger
    general_ledger = []
    for led in ledgers:
        running, entries = 0.0, []
        for t in (t for t in in_range if t.ledger_id == led.id):
            amt = float(t.amount)
            running += amt
            entries.append(
                {
                    "id": str(t.id),
                    "date": t.transaction_date.isoformat(),
                    "description": t.merchant_name or t.description or "—",
                    "debit": amt if amt > 0 else 0,
                    "credit": abs(amt) if amt < 0 else 0,
                    "balance": running,
                    "status": t.status or "posted",
                    "accounting_status": t.accounting_status,
                }
            )
        if entries:
            general_ledger.append(
                {
                    "id": str(led.id),
                    "label": led.label,
                    "account_class": led.account_class,
                    "entries": entries,
                    "closing": running,
                }
            )

    # ---- Trial balance
    trial_rows = []
    for led in ledgers:
        debit = credit = 0.0
        for t in (t for t in statement_tx if t.ledger_id == led.id):
            amt = float(t.amount)
            if amt > 0:
                debit += amt
            else:
                credit += abs(amt)
        trial_rows.append({"id": str(led.id), "label": led.label, "account_class": led.account_class, "debit": debit, "credit": credit})
    un_debit = sum(float(t.amount) for t in statement_tx if not t.ledger_id and t.amount > 0)
    un_credit = sum(abs(float(t.amount)) for t in statement_tx if not t.ledger_id and t.amount < 0)
    if un_debit or un_credit:
        trial_rows.append(
            {"id": "unassigned", "label": "Unassigned transactions", "account_class": "other", "debit": un_debit, "credit": un_credit}
        )
    class_order = ["current_asset", "noncurrent_asset", "current_liability", "noncurrent_liability", "equity", "other"]
    trial_balance = {
        "rows": sorted(
            (r for r in trial_rows if r["debit"] or r["credit"]),
            key=lambda r: (class_order.index(r["account_class"]), r["label"]),
        ),
        "total_debit": sum(r["debit"] for r in trial_rows),
        "total_credit": sum(r["credit"] for r in trial_rows),
    }

    # ---- Aging
    def build_aging(status: str):
        rows = []
        for led in ledgers:
            buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
            for t in (t for t in open_items if t.ledger_id == led.id and t.accounting_status == status):
                ref = t.due_date or t.transaction_date
                buckets[_aging_bucket(max(0, (date_to - ref).days))] += abs(float(t.amount))
            total = sum(buckets.values())
            if total > 0:
                rows.append({"id": str(led.id), "label": led.label, "contact": led.contact_name, "buckets": buckets, "total": total})
        rows.sort(key=lambda r: -r["total"])
        totals = {
            "buckets": {k: sum(r["buckets"][k] for r in rows) for k in ("0-30", "31-60", "61-90", "90+")},
            "total": sum(r["total"] for r in rows),
        }
        return {"rows": rows, "totals": totals}

    # ---- Account detail
    account_label = {a["id"]: a["label"] for a in cash_accounts}
    detail: dict = {}
    for t in in_range:
        key = t.plaid_account_id or "manual"
        entry = detail.setdefault(key, {"label": account_label.get(key, "Manual entries"), "rows": [], "total": 0.0})
        entry["rows"].append(
            {
                "id": str(t.id),
                "date": t.transaction_date.isoformat(),
                "description": t.merchant_name or t.description or "—",
                "category": cat_name(t),
                "ledger": ledger_by_id[t.ledger_id].label if t.ledger_id in ledger_by_id else "—",
                "status": t.status or "posted",
                "accounting_status": t.accounting_status,
                "amount": float(t.amount),
            }
        )
        entry["total"] += float(t.amount)

    return {
        "profit_and_loss": profit_and_loss,
        "balance_sheet": balance_sheet,
        "cash_flow": cash_flow,
        "general_ledger": general_ledger,
        "trial_balance": trial_balance,
        "ar_aging": build_aging("accounts_receivable"),
        "ap_aging": build_aging("accounts_payable"),
        "account_detail": [{"id": k, **v} for k, v in detail.items()],
    }
