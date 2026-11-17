"""Reports computed from double-entry journal lines, plus a side-by-side check against the current (single-entry) reports.

Nothing here replaces the existing reports yet: `compare_with_current` is how we prove the two agree before switching.
All amounts are floats rounded to cents so they line up with the existing report payloads.
"""

from datetime import date

from django.db.models import Sum

from apps.banking.models import Reconciliation
from apps.banking.services.reconcile import statement_sign, summary
from apps.ledger.models import Account, JournalLine
from apps.ledger.services.journal import CLEARING

from .accounting import accounting_reports


def _totals(user, *, date_to: date, date_from: date | None = None) -> dict:
    qs = JournalLine.objects.filter(entry__user=user, entry__date__lte=date_to)
    if date_from:
        qs = qs.filter(entry__date__gte=date_from)
    rows = qs.values("account_id").annotate(d=Sum("debit"), c=Sum("credit"))
    return {r["account_id"]: (float(r["d"] or 0), float(r["c"] or 0)) for r in rows}


def entries_reports(user, *, date_from: date, date_to: date) -> dict:
    accounts = {a.id: a for a in Account.objects.filter(user=user)}
    upto = _totals(user, date_to=date_to)  # balances: everything up to the report date
    period = _totals(user, date_to=date_to, date_from=date_from)  # profit: only inside the range

    def lines(kind, source, credit_normal):
        out = []
        for aid, (d, c) in source.items():
            a = accounts[aid]
            if a.kind == kind:
                v = round((c - d) if credit_normal else (d - c), 2)
                if v:
                    out.append({"id": str(a.id), "code": a.code, "name": a.name, "balance": v})
        return sorted(out, key=lambda r: r["code"])

    income, expense = lines("income", period, True), lines("expense", period, False)
    total_income, total_expenses = round(sum(r["balance"] for r in income), 2), round(sum(r["balance"] for r in expense), 2)

    assets, liabilities = lines("asset", upto, False), lines("liability", upto, True)
    equity = lines("equity", upto, True)
    to_date_income = round(sum(r["balance"] for r in lines("income", upto, True)), 2)
    to_date_expense = round(sum(r["balance"] for r in lines("expense", upto, False)), 2)
    retained = round(to_date_income - to_date_expense, 2)
    total_assets, total_liabilities = round(sum(r["balance"] for r in assets), 2), round(sum(r["balance"] for r in liabilities), 2)
    total_equity = round(sum(r["balance"] for r in equity) + retained, 2)

    tb_rows = []
    for aid, (d, c) in upto.items():
        net = round(d - c, 2)
        if net:
            a = accounts[aid]
            tb_rows.append(
                {"code": a.code, "name": a.name, "kind": a.kind, "debit": net if net > 0 else 0.0, "credit": -net if net < 0 else 0.0}
            )
    tb_rows.sort(key=lambda r: r["code"])
    debit, credit = round(sum(r["debit"] for r in tb_rows), 2), round(sum(r["credit"] for r in tb_rows), 2)

    clearing = next((a for a in accounts.values() if a.system_key == CLEARING.key[1]), None)
    out_, in_ = (
        period.get(clearing.id, (0.0, 0.0)) if clearing else (0.0, 0.0)
    )  # money out debits the clearing account, money in credits it
    return {
        "internal_transfers": {"money_out": round(out_, 2), "money_in": round(in_, 2), "difference": round(in_ - out_, 2)},
        "profit_and_loss": {
            "income": income,
            "expenses": expense,
            "total_income": total_income,
            "total_expenses": total_expenses,
            "net_profit": round(total_income - total_expenses, 2),
        },
        "balance_sheet": {
            "assets": assets,
            "liabilities": liabilities,
            "equity": [*equity, {"id": "retained", "code": "", "name": "Retained earnings", "balance": retained}],
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "total_equity": total_equity,
            "unbalanced": round(total_assets - (total_liabilities + total_equity), 2),
        },
        "trial_balance": {"rows": tb_rows, "total_debit": debit, "total_credit": credit, "balanced": debit == credit},
    }


def compare_with_current(user, *, date_from: date, date_to: date) -> dict:
    """Each headline number from the current reports next to the same number from the journal."""
    cur = accounting_reports(user, date_from=date_from, date_to=date_to, balance_sheet_from_journal=False)
    new = entries_reports(user, date_from=date_from, date_to=date_to)
    pl, bs = cur["profit_and_loss"], cur["balance_sheet"]
    sales_tax_now = next((r["balance"] for r in bs["current_liabilities"] if r.get("kind") == "sales_tax"), 0.0)
    sales_tax_new = next((r["balance"] for r in new["balance_sheet"]["liabilities"] if r["name"] == "Sales Tax Payable"), 0.0)
    pairs = [
        ("Total income", pl["total_income"], new["profit_and_loss"]["total_income"]),
        ("Total expenses", pl["total_expenses"], new["profit_and_loss"]["total_expenses"]),
        ("Net profit", pl["net_profit"], new["profit_and_loss"]["net_profit"]),
        ("Total assets", bs["total_assets"], new["balance_sheet"]["total_assets"]),
        ("Total liabilities", bs["total_liabilities"], new["balance_sheet"]["total_liabilities"]),
        ("Total equity", bs["total_equity"], new["balance_sheet"]["total_equity"]),
        ("Sales tax payable", sales_tax_now, sales_tax_new),
        ("Balance sheet out of balance by", bs["unbalanced"], new["balance_sheet"]["unbalanced"]),
        ("Transfers: money out", cur["internal_transfers"]["money_out"], new["internal_transfers"]["money_out"]),
        ("Transfers: money in", cur["internal_transfers"]["money_in"], new["internal_transfers"]["money_in"]),
        ("Transfers: difference", cur["internal_transfers"]["difference"], new["internal_transfers"]["difference"]),
    ]
    rows = [{"measure": m, "current": round(a, 2), "entries": round(b, 2), "difference": round(b - a, 2)} for m, a, b in pairs]
    return {"rows": rows, "matches": all(abs(r["difference"]) < 0.005 for r in rows), "entries": new}


def reconciliation_check(user) -> dict:
    """For every reconciliation: the cleared activity as the reconciliation screen computes it, against the same rows read from the
    journal's lines on that bank account. They must agree: it proves a reconciliation can run on entries."""
    rows = []
    for rec in Reconciliation.objects.filter(user=user).select_related("bank_account"):
        s = summary(rec)
        from_transactions = s["additions"] - s["subtractions"]
        account = Account.objects.filter(user=user, plaid_account_id=rec.bank_account.plaid_account_id).first()
        t = JournalLine.objects.filter(account=account, entry__transaction__reconciliation=rec).aggregate(d=Sum("debit"), c=Sum("credit"))
        net = (t["d"] or 0) - (t["c"] or 0)
        from_journal = net * statement_sign(rec.bank_account)  # a card or loan statement counts credits as plus
        rows.append(
            {
                "account": rec.bank_account.account_name,
                "statement_date": rec.statement_date.isoformat(),
                "status": rec.status,
                "cleared_from_transactions": float(from_transactions),
                "cleared_from_journal": float(from_journal),
                "difference": round(float(from_journal - from_transactions), 2),
            }
        )
    return {"rows": rows, "matches": all(r["difference"] == 0 for r in rows)}
