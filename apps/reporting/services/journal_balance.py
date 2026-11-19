"""The balance sheet built from the double-entry journal, in the same shape the page already reads, plus the "books health check".

The journal always balances, so this sheet cannot be out of balance. That does not make it right: the health check shows the things
that can still be wrong (a bank balance that disagrees, transfers with no partner, rows with no entry, money not yet in a bank).
"""

from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.banking.models import BankAccount
from apps.ledger.models import Account, JournalEntry, JournalLine, Ledger, Transaction
from apps.ledger.services.journal import (
    CLEARING,
    GHL_CLEARING,
    MANUAL_CASH,
    OPENING_EQUITY,
    OPENING_KINDS,
    PAYABLE,
    RECEIVABLE,
    UNCATEGORIZED,
    Context,
    lines_for,
)

CENT = Decimal("0.01")
SALES_TAX_ACCOUNT = "Sales Tax Payable"
# Built-in accounts whose balance-sheet line opens the transactions posted to them.
BOOKS_ACCOUNT_KEYS = (MANUAL_CASH.key[1], *(r.key[1] for r in UNCATEGORIZED.values()))


def has_entries(user) -> bool:
    return JournalEntry.objects.filter(user=user).exists()


def _sums(user, *, date_to: date | None = None) -> dict:
    qs = JournalLine.objects.filter(entry__user=user)
    if date_to:
        qs = qs.filter(entry__date__lte=date_to)
    return {
        r["account_id"]: (r["d"] or Decimal(0), r["c"] or Decimal(0))
        for r in qs.values("account_id").annotate(d=Sum("debit"), c=Sum("credit"))
    }


def _by_ledger(user, *, date_to: date) -> dict:
    """Receivable and payable balances split by the customer/vendor ledger each line belongs to: {account_id: {ledger_id: (d, c)}}."""
    qs = JournalLine.objects.filter(entry__user=user, entry__date__lte=date_to, account__system_key__in=(RECEIVABLE.key[1], PAYABLE.key[1]))
    out: dict = {}
    for r in qs.values("account_id", "ledger_id").annotate(d=Sum("debit"), c=Sum("credit")):
        out.setdefault(r["account_id"], {})[r["ledger_id"]] = (r["d"] or Decimal(0), r["c"] or Decimal(0))
    return out


def _f(value: Decimal) -> float:
    return float(round(value, 2))


def balance_sheet(user, *, date_to: date) -> dict:
    """Assets, liabilities and equity as of `date_to`, from journal lines. Same keys and line shape as the single-entry sheet."""
    sums = _sums(user, date_to=date_to)
    bank_types = {b.plaid_account_id: (b.account_type or "").lower() for b in BankAccount.objects.for_user(user)}
    groups: dict[str, list] = {"ca": [], "nca": [], "cl": [], "ncl": [], "eq": []}
    split = _by_ledger(user, date_to=date_to)
    ledgers = {led.id: led for led in Ledger.objects.for_user(user)}
    class_group = {"current_asset": "ca", "noncurrent_asset": "nca", "current_liability": "cl", "noncurrent_liability": "ncl"}
    income = expense = Decimal(0)

    for a in Account.objects.filter(user=user).order_by("code"):
        d, c = sums.get(a.id, (Decimal(0), Decimal(0)))
        if a.kind == "income":
            income += c - d
            continue
        if a.kind == "expense":
            expense += d - c
            continue
        balance = (d - c) if a.kind == "asset" else (c - d)  # liabilities and equity are shown as amounts owed / held
        if a.id in split:
            # Receivable / payable: one line per customer or vendor ledger (so its transactions can be opened), plus the rest.
            rest = balance
            for ledger_id, (ld, lc) in split[a.id].items():
                part = (ld - lc) if a.kind == "asset" else (lc - ld)
                led = ledgers.get(ledger_id)
                if led is None or not part:
                    continue
                rest -= part
                side = ("ca", "nca") if a.kind == "asset" else ("cl", "ncl")
                group = class_group.get(led.account_class)
                groups[group if group in side else side[0]].append(
                    {"id": str(led.id), "label": led.label, "balance": _f(part), "kind": "ledger"}
                )
            if rest:
                groups["ca" if a.kind == "asset" else "cl"].append(
                    {"id": str(a.id), "label": a.name, "balance": _f(rest), "kind": "receivable_rest" if a.kind == "asset" else "payable_rest"}
                )
            continue
        is_bank = bool(a.plaid_account_id)
        if not balance and not (is_bank and a.kind != "equity"):
            continue  # bank and card accounts always show, even at zero, as before
        line = {"id": str(a.id), "label": a.name, "balance": _f(balance)}
        if is_bank:
            line.update(id=a.plaid_account_id, kind="bank")
        elif a.system_key == OPENING_EQUITY.key[1]:
            line.update(id="opening_balance", kind="opening_balance")
        elif a.category_id:
            line.update(id=str(a.category_id), kind="category")
        elif a.system_key == CLEARING.key[1]:
            line.update(id="transfers_in_transit", kind="transfers_in_transit")
        elif a.system_key == GHL_CLEARING.key[1]:
            line.update(id="ghl_clearing", kind="ghl_clearing")
        elif a.system_key in BOOKS_ACCOUNT_KEYS:
            line.update(id=a.system_key, kind="books_account")  # opens the transactions posted to this built-in account
        elif a.kind == "liability" and a.name == SALES_TAX_ACCOUNT:
            line.update(id="sales_tax", kind="sales_tax")
        if a.kind == "asset":
            groups["ca"].append(line)
        elif a.kind == "liability":
            groups["ncl" if bank_types.get(a.plaid_account_id) == "loan" else "cl"].append(line)
        else:
            groups["eq"].append(line)

    retained = round(income - expense, 2)
    equity = [*groups["eq"], {"id": "retained", "label": "Retained earnings", "balance": _f(retained), "kind": "retained"}]
    total = lambda rows: round(sum(r["balance"] for r in rows), 2)  # noqa: E731
    total_assets = round(total(groups["ca"]) + total(groups["nca"]), 2)
    total_liabilities = round(total(groups["cl"]) + total(groups["ncl"]), 2)
    total_equity = total(equity)
    return {
        "current_assets": groups["ca"],
        "noncurrent_assets": groups["nca"],
        "current_liabilities": groups["cl"],
        "noncurrent_liabilities": groups["ncl"],
        "equity": equity,
        "total_current_assets": total(groups["ca"]),
        "total_noncurrent_assets": total(groups["nca"]),
        "total_current_liabilities": total(groups["cl"]),
        "total_noncurrent_liabilities": total(groups["ncl"]),
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
        "unbalanced": round(total_assets - (total_liabilities + total_equity), 2),
    }


def _money(value: Decimal) -> str:
    return f"${abs(value):,.2f}"


def books_check(user, *, date_to: date) -> dict:
    """Plain-language checks on whether the books are right (not only balanced). Each check is ok or needs a look."""
    upto = _sums(user, date_to=date_to)
    everything = _sums(user)
    accounts = {a.id: a for a in Account.objects.filter(user=user)}
    by_key = {a.system_key: a for a in accounts.values() if a.system_key}

    # 1. Each bank account: what the journal says against what the bank says (today).
    banks, off = [], 0
    for b in BankAccount.objects.for_user(user).order_by("account_name"):
        acc = next((a for a in accounts.values() if a.plaid_account_id == b.plaid_account_id), None)
        if acc is None or b.current_balance is None:
            continue
        d, c = everything.get(acc.id, (Decimal(0), Decimal(0)))
        books = (c - d) if acc.kind == "liability" else (d - c)
        diff = (books - Decimal(b.current_balance)).quantize(CENT)
        if diff:
            off += 1
        banks.append(
            {"account": acc.name, "books": _f(books), "bank": _f(Decimal(b.current_balance)), "difference": _f(diff), "ok": not diff}
        )

    # 2. Transfers between the user's own accounts that have no partner yet.
    clearing = by_key.get(CLEARING.key[1])
    d, c = upto.get(clearing.id, (Decimal(0), Decimal(0))) if clearing else (Decimal(0), Decimal(0))
    in_transit = (d - c).quantize(CENT)

    # 3. Transactions that should have an entry but do not (a failed posting is logged, never shown).
    ctx = Context(user)
    missing = 0
    for tx in Transaction.objects.for_user(user).filter(removed_at__isnull=True, journal_entries__isnull=True).iterator():
        lines, _ = lines_for(tx, ctx)
        if lines is not None:
            missing += 1

    # 4. Money collected through GoHighLevel that has not reached a bank account.
    ghl = by_key.get(GHL_CLEARING.key[1])
    d, c = upto.get(ghl.id, (Decimal(0), Decimal(0))) if ghl else (Decimal(0), Decimal(0))
    ghl_balance = (d - c).quantize(CENT)

    # 5. Starting balances: flagged when larger than all the money recorded moving through that account.
    openings = []
    rows = (
        JournalLine.objects.filter(entry__user=user, entry__kind__in=OPENING_KINDS, account__plaid_account_id__isnull=False)
        .values("account_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    for r in rows:
        acc = accounts[r["account_id"]]
        amount = (r["c"] - r["d"]) if acc.kind == "liability" else (r["d"] - r["c"])
        activity = JournalLine.objects.filter(account=acc).exclude(entry__kind__in=OPENING_KINDS).aggregate(d=Sum("debit"), c=Sum("credit"))
        gross = (activity["d"] or Decimal(0)) + (activity["c"] or Decimal(0))
        openings.append({"account": acc.name, "amount": _f(amount), "large": abs(amount) >= 1000 and abs(amount) > gross})
    large = [o for o in openings if o["large"]]

    def check(key, title, ok, detail):
        return {"id": key, "title": title, "status": "ok" if ok else "look", "detail": detail}

    checks = [
        check(
            "banks",
            "Bank balances match",
            not off,
            "Every bank and card account agrees with the balance the bank reports."
            if not off
            else f"{off} account{'s' if off != 1 else ''} differ from the bank's balance. Some transactions may be missing from the history.",
        ),
        check(
            "transfers",
            "Transfers between your own accounts all have two sides",
            not in_transit,
            "No transfer is waiting for its other half."
            if not in_transit
            else f"{_money(in_transit)} {'left an account' if in_transit < 0 else 'arrived'} with no matching "
            f"{'arrival' if in_transit < 0 else 'departure'} in another account. Check the Internal transfers report.",
        ),
        check(
            "missing",
            "Every transaction is in the books",
            not missing,
            "No transactions are missing a journal entry."
            if not missing
            else f"{missing} transaction{'s are' if missing != 1 else ' is'} not in the books. Ask for the backfill to be run.",
        ),
        check(
            "ghl",
            "GoHighLevel money has reached a bank",
            not ghl_balance,
            "Nothing is waiting between GoHighLevel and the bank."
            if not ghl_balance
            else f"{_money(ghl_balance)} was collected through GoHighLevel and is not yet matched to a bank deposit.",
        ),
        check(
            "opening",
            "Starting balances look reasonable",
            not large,
            "Each starting balance is smaller than the activity recorded on its account."
            if not large
            else f"{len(large)} account{'s have' if len(large) != 1 else ' has'} a starting balance bigger than all the activity we have, "
            "so older history may be missing.",
        ),
    ]
    return {"all_ok": all(c["status"] == "ok" for c in checks), "checks": checks, "banks": banks, "openings": openings}
