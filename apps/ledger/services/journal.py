"""Double entry: turns a transaction into a balanced journal entry (expand stage: nothing reads these entries yet).

Every entry has two or more lines whose debits equal their credits. For a bank-synced row the bank account is one side and
the category's account is the other:

    money in  (+100 income):    debit  bank 100,  credit income 100
    money out (-40 expense):    debit  expense 40, credit bank 40
    card purchase (-40):        debit  expense 40, credit card (a liability) 40

The two sides of a transfer between the user's own accounts each post against "Transfers in transit", which nets to zero once
both sides are in. A balance left there is a transfer whose other side is missing.

GoHighLevel invoices post on the accrual basis, against "Accounts receivable" and "Collected through GoHighLevel, not yet in a bank account":

    invoice revenue:   debit receivable, credit revenue      sales tax:  debit receivable, credit sales tax payable
    payment received:  debit clearing,   credit receivable   fee:        debit expense,    credit clearing
    refund:            debit revenue,    credit clearing

The "balance due" row GoHighLevel keeps per invoice only restates the open balance, so it is not posted. Instead the backfill
checks that the receivable account equals the sum of those open balances.

Manual entries: money in debits "Accounts receivable" when the entry is marked receivable, otherwise "Cash - manual entries"
(the entry names no bank). Money out credits "Accounts payable" when marked payable, otherwise the same cash account.
"""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.banking.models import BankAccount

from ..models import Account, Category, JournalEntry, JournalLine, Transaction
from .gaap import row_gaap_type

log = logging.getLogger(__name__)

KIND_FOR_GAAP = {
    "revenue": "income",
    "other_income": "income",
    "cogs": "expense",
    "operating_expense": "expense",
    "other_expense": "expense",
    "asset": "asset",
    "liability": "liability",
    "equity": "equity",
}
CODE_BASE = {"asset": 1000, "liability": 2000, "equity": 3000, "income": 4000, "expense": 5000}
BANK_SOURCES = ("plaid", "csv")
LIABILITY_BANK_TYPES = ("credit", "loan")


@dataclass(frozen=True)
class Ref:
    """Which account a line belongs to, before the account row exists. `key` is unique per account for one user."""

    key: tuple
    kind: str
    name: str


CLEARING = Ref(("system", "transfer_clearing"), "asset", "Transfers in transit")
UNCAT_INCOME = Ref(("system", "uncategorized_income"), "income", "Uncategorized income")
UNCAT_EXPENSE = Ref(("system", "uncategorized_expense"), "expense", "Uncategorized expense")
UNCATEGORIZED = {
    "income": UNCAT_INCOME,
    "expense": UNCAT_EXPENSE,
    "asset": Ref(("system", "uncategorized_asset"), "asset", "Uncategorized asset"),
    "liability": Ref(("system", "uncategorized_liability"), "liability", "Uncategorized liability"),
    "equity": Ref(("system", "uncategorized_equity"), "equity", "Uncategorized equity"),
}
OPENING_EQUITY = Ref(("system", "opening_balance"), "equity", "Opening balance equity")
RECEIVABLE = Ref(("system", "receivable"), "asset", "Accounts receivable")
PAYABLE = Ref(("system", "payable"), "liability", "Accounts payable")
GHL_CLEARING_LABEL = "Collected through GoHighLevel, not yet in a bank account"
GHL_CLEARING = Ref(("system", "ghl_clearing"), "asset", GHL_CLEARING_LABEL)
MANUAL_CASH = Ref(("system", "manual_cash"), "asset", "Cash - manual entries (no bank chosen)")


class Context:
    """What the mapping needs to look up. `preload=True` loads everything once (for many rows); otherwise it looks up on demand."""

    def __init__(self, user, *, preload: bool = True):
        self.user = user
        self.preload = preload
        self.banks: dict = {}
        self.categories: dict = {}
        if preload:
            self.banks = {a.plaid_account_id: a for a in BankAccount.objects.for_user(user)}
            self.categories = {c.id: c for c in Category.objects.filter(Q(user=user) | Q(user__isnull=True))}

    def _bank(self, plaid_account_id):
        if plaid_account_id not in self.banks and not self.preload:
            self.banks[plaid_account_id] = BankAccount.objects.for_user(self.user).filter(plaid_account_id=plaid_account_id).first()
        return self.banks.get(plaid_account_id)

    def _category(self, category_id):
        if category_id not in self.categories and not self.preload:
            self.categories[category_id] = Category.objects.filter(Q(user=self.user) | Q(user__isnull=True), pk=category_id).first()
        return self.categories.get(category_id)

    def bank_ref(self, plaid_account_id):
        a = self._bank(plaid_account_id)
        if a is None:
            if not plaid_account_id:
                return None
            # The row names a bank account we have no record of (for example a disconnected bank). Post it to a placeholder
            # that can be renamed later, so the row is not lost from the books.
            return Ref(("bank", plaid_account_id), "asset", f"Bank account not on file ({plaid_account_id[-6:]})")
        kind = "liability" if (a.account_type or "").lower() in LIABILITY_BANK_TYPES else "asset"
        label = f"{a.account_name or 'Account'}{f' ••{a.mask}' if a.mask else ''}"
        return Ref(("bank", plaid_account_id), kind, label)

    def category_ref(self, tx, category_id=None):
        """The account for a transaction's category (or, for one part of a split, that part's own category)."""
        category_id = category_id or tx.user_category_id
        c = self._category(category_id) if category_id else None
        kind = KIND_FOR_GAAP.get(c.type) if c else None
        row_kind = KIND_FOR_GAAP.get(tx.gaap_type)
        # The row's own type decides which side of the books it belongs on (profit vs balance sheet), as in the current reports:
        # a category of another kind, or no category at all, falls back to "Uncategorized <that kind>".
        if row_kind and (c is None or kind != row_kind):
            return UNCATEGORIZED[row_kind]
        if c is None or kind is None:
            return UNCAT_INCOME if tx.amount > 0 else UNCAT_EXPENSE
        return Ref(("category", c.id), kind, c.name)


def lines_for(tx: Transaction, ctx: Context):
    """(lines, None) where lines is [(Ref, debit, credit), ...], or (None, reason) when the row is not posted."""
    if tx.removed_at:
        return None, "removed"
    if tx.is_hidden:
        return None, "hidden"
    if tx.pending:
        return None, "pending"
    if tx.status == "void":
        return None, "void"
    if tx.amount == 0:
        return None, "zero"
    if tx.source in BANK_SOURCES:
        return _bank_lines(tx, ctx)
    if tx.source == "ghl":
        return _ghl_lines(tx, ctx)
    if tx.source == "manual":
        return _manual_lines(tx, ctx)
    return None, f"source:{tx.source}"


def _two(amount: Decimal, debit: Ref, credit: Ref):
    """A positive amount debits `debit` and credits `credit`; a negative amount is the reverse."""
    a = abs(Decimal(amount))
    if amount > 0:
        return [(debit, a, Decimal(0)), (credit, Decimal(0), a)], None
    return [(credit, a, Decimal(0)), (debit, Decimal(0), a)], None


def usable_splits(tx):
    """The transaction's parts, or [] when it has none or they no longer add up to its amount (then it is posted whole)."""
    parts = list(tx.splits.all())
    return parts if parts and sum(p.amount for p in parts) == tx.amount else []


def _split_two(tx, ctx, side: Ref, parts):
    """`side` (the bank, receivable, ...) carries the whole amount; the other side is one line per part, so each part keeps its own
    category, project and ledger. A positive amount debits `side`, a negative one credits it. A positive part credits its account
    and a negative part (a credit memo on a statement) debits it, so the entry balances whatever the mix."""
    total = abs(Decimal(tx.amount))
    out = [(side, total, Decimal(0)) if tx.amount > 0 else (side, Decimal(0), total)]
    for p in parts:
        a = abs(Decimal(p.amount))
        ref = ctx.category_ref(tx, p.category_id)
        extra = {"project_id": p.project_id if ref.kind in ("income", "expense") else None, "ledger_id": p.ledger_id or tx.ledger_id}
        out.append((ref, Decimal(0), a, extra) if p.amount > 0 else (ref, a, Decimal(0), extra))
    return out, None


def _is_transfer(tx) -> bool:
    return bool(tx.is_transfer or tx.gaap_type == "transfer")


def _bank_lines(tx, ctx):
    bank = ctx.bank_ref(tx.plaid_account_id)
    if bank is None:
        return None, "no_bank_account"
    if not _is_transfer(tx) and (parts := usable_splits(tx)):
        return _split_two(tx, ctx, bank, parts)
    other = CLEARING if _is_transfer(tx) else ctx.category_ref(tx)
    return _two(tx.amount, bank, other)


def _ghl_lines(tx, ctx):
    ref = tx.external_ref or ""
    if ref.endswith(":ar"):
        return None, "ghl_balance_view"
    cat = ctx.category_ref(tx)
    if ref.startswith("ghl:inv:") and ref.endswith((":revenue", ":tax")):
        return _two(tx.amount, RECEIVABLE, cat)
    if ref.startswith("ghl:pay:"):
        return _two(tx.amount, GHL_CLEARING, RECEIVABLE)
    if ref.startswith(("ghl:fee:", "ghl:refund:")):
        return _two(tx.amount, GHL_CLEARING, cat)
    return None, "ghl_unknown"


def _manual_lines(tx, ctx):
    if tx.amount > 0:
        side = RECEIVABLE if tx.accounting_status == "accounts_receivable" else MANUAL_CASH
    else:
        side = PAYABLE if tx.accounting_status == "accounts_payable" else MANUAL_CASH
    if not _is_transfer(tx) and (parts := usable_splits(tx)):
        return _split_two(tx, ctx, side, parts)
    other = CLEARING if _is_transfer(tx) else ctx.category_ref(tx)
    return _two(tx.amount, side, other)


def _resolve_account(user, ref: Ref) -> Account:
    """The Account row for a ref, created (with the next free code for its kind) the first time it is needed."""
    where = {"user": user}
    kind_key, key = ref.key
    if kind_key == "bank":
        where["plaid_account_id"] = key
    elif kind_key == "category":
        where["category_id"] = key
    else:
        where["system_key"] = key
    found = Account.objects.filter(**where).first()
    if found:
        if kind_key == "system" and found.name != ref.name:  # built-in accounts follow the current wording
            found.name = ref.name
            found.save(update_fields=["name"])
        return found
    used = Account.objects.filter(user=user, kind=ref.kind).count()
    code = CODE_BASE[ref.kind] + 10 * (used + 1)
    while Account.objects.filter(user=user, code=str(code)).exists():
        code += 1
    return Account.objects.create(
        user=user, code=str(code), name=ref.name, kind=ref.kind, **{k: v for k, v in where.items() if k != "user"}
    )


def _cached_account(user, ref: Ref, cache: dict) -> Account:
    if ref.key not in cache:
        cache[ref.key] = _resolve_account(user, ref)
    return cache[ref.key]


def post_transaction(user, tx: Transaction, ctx: Context, cache: dict) -> str | None:
    """Writes (or rewrites) the transaction's journal entry. Returns None when posted, else why it was skipped."""
    lines, reason = lines_for(tx, ctx)
    JournalEntry.objects.filter(transaction=tx, kind="auto").delete()  # rebuilt below: re-posting is safe to repeat
    if lines is None:
        return reason
    entry = JournalEntry.objects.create(user=user, date=tx.transaction_date, memo=tx.payee or tx.merchant_name or tx.description, transaction=tx)
    JournalLine.objects.bulk_create(
        [
            JournalLine(
                entry=entry,
                account=_cached_account(user, ref, cache),
                debit=debit,
                credit=credit,
                # a line may carry its own project/ledger (the parts of a split); otherwise the transaction's
                project_id=extra[0]["project_id"] if extra else (tx.project_id if ref.kind in ("income", "expense") else None),
                ledger_id=extra[0]["ledger_id"] if extra else tx.ledger_id,
                gaap_type=row_gaap_type(tx) if ref.kind in ("income", "expense") else None,
            )
            for ref, debit, credit, *extra in lines
        ]
    )
    return None


def repost(ids) -> None:
    """Rewrites the entries of the given transactions after a bulk write that did not go through save(). Never raises: the
    transaction write that triggered it must not fail because of the books; `backfill_journal` repairs anything missed."""
    ids = list(ids)
    if not ids:
        return
    by_user: dict = defaultdict(list)
    for tx in Transaction.objects.filter(id__in=ids).prefetch_related("splits"):
        by_user[tx.user_id].append(tx)
    for rows in by_user.values():
        user = rows[0].user
        ctx, cache = Context(user, preload=len(rows) > 20), {}
        for tx in rows:
            try:
                with db_transaction.atomic():
                    post_transaction(user, tx, ctx, cache)
            except Exception:  # noqa: BLE001 - see docstring
                log.exception("journal: could not post transaction %s", tx.pk)


def update_and_repost(qs, **changes) -> int:
    """qs.update(**changes), then keeps the journal in step. The ids are taken first because the update may change what qs matches."""
    ids = list(qs.values_list("id", flat=True))
    n = Transaction.objects.filter(id__in=ids).update(**changes)
    repost(ids)
    return n


OPENING_KINDS = ("opening", "opening_set")  # worked out from the bank's balance | typed in by the user


def _post_opening(user, ref: Ref, delta: Decimal, day, kind: str, cache: dict) -> None:
    """One opening entry: `delta` is debits less credits on the bank account; the other side is "Opening balance equity"."""
    bank_account = _cached_account(user, ref, cache)
    equity = _cached_account(user, OPENING_EQUITY, cache)
    entry = JournalEntry.objects.create(user=user, date=day, memo=f"Opening balance: {ref.name}", kind=kind)
    amount = abs(delta)
    sides = (
        ((bank_account, amount, Decimal(0)), (equity, Decimal(0), amount))
        if delta > 0
        else ((equity, amount, Decimal(0)), (bank_account, Decimal(0), amount))
    )
    JournalLine.objects.bulk_create([JournalLine(entry=entry, account=a, debit=d, credit=c) for a, d, c in sides])


def opening_balances(user, *, apply: bool = False, only: str | None = None) -> list[dict]:
    """What each bank account held before the first transaction we have, so the books agree with the bank.

    For every bank account with a balance from the bank: opening = the bank's balance less what the posted transactions add up to.
    It is posted dated the day before the account's first entry (debit the account, credit "Opening balance equity", or the reverse
    for a negative amount). Safe to repeat: the previous opening entry for the account is replaced. A large opening amount means
    transactions are missing from the history, so each row is reported rather than hidden.

    An opening balance the user typed in (kind "opening_set") is never replaced here: the row is reported with `manual` set.
    `only` limits the work to one bank account (its plaid_account_id).
    """
    ctx, cache, out = Context(user), {}, []
    banks = BankAccount.objects.for_user(user).order_by("account_name")
    if only:
        banks = banks.filter(plaid_account_id=only)
    for bank in banks:
        ref = ctx.bank_ref(bank.plaid_account_id)
        account = Account.objects.filter(user=user, plaid_account_id=bank.plaid_account_id).first()
        sign = -1 if ref.kind == "liability" else 1  # liabilities are shown as the amount owed
        posted = JournalLine.objects.filter(account=account, entry__kind__in=OPENING_KINDS) if account else JournalLine.objects.none()
        cur = posted.aggregate(d=Sum("debit"), c=Sum("credit"))
        current_entry = posted.select_related("entry").first()
        manual = bool(current_entry and current_entry.entry.kind == "opening_set")
        base = {
            "plaid_account_id": bank.plaid_account_id,
            "account": ref.name,
            "current": sign * ((cur["d"] or Decimal(0)) - (cur["c"] or Decimal(0))) if current_entry else None,
            "current_date": current_entry.entry.date if current_entry else None,
            "manual": manual,
        }
        lines = JournalLine.objects.filter(account=account).exclude(entry__kind__in=OPENING_KINDS) if account else JournalLine.objects.none()
        total = lines.aggregate(d=Sum("debit"), c=Sum("credit"))
        net_debit = (total["d"] or Decimal(0)) - (total["c"] or Decimal(0))
        books = sign * net_debit
        first = lines.order_by("entry__date").values_list("entry__date", flat=True).first()
        day = (first - timedelta(days=1)) if first else timezone.localdate()
        if bank.current_balance is None:
            out.append({**base, "from_transactions": books, "date": day, "skipped": "no balance from the bank"})
            continue
        needed = Decimal(bank.current_balance) - books  # in the account's own terms (liabilities: amount owed)
        delta = sign * needed  # as debits less credits on the account
        out.append({**base, "bank_balance": Decimal(bank.current_balance), "from_transactions": books, "opening": needed, "date": day})
        if not apply or manual:
            continue
        with db_transaction.atomic():
            if account:
                JournalEntry.objects.filter(user=user, kind="opening", lines__account=account).delete()
            if delta == 0:
                continue
            _post_opening(user, ref, delta, day, "opening", cache)
    return out


def set_opening_balance(user, plaid_account_id: str, amount: Decimal, day=None) -> None:
    """Records the opening balance the user typed in for one bank account, replacing whatever opening entry it had.

    `amount` is in the account's own terms: money held for a bank account, the amount owed for a card or loan. `day` defaults to
    the day before the account's first entry. The automatic step (`opening_balances`) leaves this entry alone from then on.
    Raises ValueError with a plain message when the account is not one of the user's.
    """
    if not BankAccount.objects.for_user(user).filter(plaid_account_id=plaid_account_id).exists():
        raise ValueError("Bank account not found.")
    ref = Context(user, preload=False).bank_ref(plaid_account_id)
    cache: dict = {}
    with db_transaction.atomic():
        account = _cached_account(user, ref, cache)
        if day is None:
            first = (
                JournalLine.objects.filter(account=account)
                .exclude(entry__kind__in=OPENING_KINDS)
                .order_by("entry__date")
                .values_list("entry__date", flat=True)
                .first()
            )
            day = (first - timedelta(days=1)) if first else timezone.localdate()
        JournalEntry.objects.filter(user=user, kind__in=OPENING_KINDS, lines__account=account).delete()
        delta = -amount if ref.kind == "liability" else amount
        if delta:
            _post_opening(user, ref, delta, day, "opening_set", cache)


def reset_opening_balance(user, plaid_account_id: str) -> None:
    """Drops the typed-in opening balance for one bank account and works it out from the bank's balance again."""
    account = Account.objects.filter(user=user, plaid_account_id=plaid_account_id).first()
    if account is None:
        raise ValueError("Bank account not found.")
    with db_transaction.atomic():
        JournalEntry.objects.filter(user=user, kind="opening_set", lines__account=account).delete()
        opening_balances(user, apply=True, only=plaid_account_id)


def ensure_category_account(user, category) -> Account | None:
    """The chart-of-accounts row for a category, created now instead of at its first transaction. None for a type with no account."""
    kind = KIND_FOR_GAAP.get(category.type)
    if kind is None:
        return None
    return _resolve_account(user, Ref(("category", category.id), kind, category.name))


def backfill(user, *, apply: bool = False) -> dict:
    """Builds entries for every existing transaction. With apply=False nothing is written: it only reports what would happen."""
    ctx = Context(user)
    rows = (
        Transaction.objects.for_user(user)
        .filter(removed_at__isnull=True)
        .prefetch_related("splits")
        .order_by("transaction_date", "created_at")
    )
    skipped: Counter = Counter()
    totals: dict = defaultdict(lambda: [Decimal(0), Decimal(0)])  # account name -> [debit, credit]
    mapped = 0
    open_invoices = Decimal(0)  # what the sources say is still owed: GHL balance-due rows plus manual receivables
    cache: dict = {}
    with db_transaction.atomic():
        for tx in rows.iterator(chunk_size=1000):
            lines, reason = lines_for(tx, ctx)
            if reason == "ghl_balance_view" or (lines and tx.source == "manual" and tx.accounting_status == "accounts_receivable"):
                open_invoices += tx.amount
            if lines is None:
                skipped[reason] += 1
                if apply:
                    JournalEntry.objects.filter(transaction=tx, kind="auto").delete()
                continue
            mapped += 1
            for ref, debit, credit, *_ in lines:
                totals[(ref.kind, ref.name)][0] += debit
                totals[(ref.kind, ref.name)][1] += credit
            if apply:
                post_transaction(user, tx, ctx, cache)
    debit = sum(t[0] for t in totals.values())
    credit = sum(t[1] for t in totals.values())
    clearing = totals.get((CLEARING.kind, CLEARING.name))
    receivable = totals.get((RECEIVABLE.kind, RECEIVABLE.name), [Decimal(0), Decimal(0)])
    books = receivable[0] - receivable[1]
    return {
        "applied": apply,
        "mapped": mapped,
        "skipped": dict(skipped),
        "total_debit": debit,
        "total_credit": credit,
        "balanced": debit == credit,
        "transfers_in_transit": (clearing[0] - clearing[1]) if clearing else Decimal(0),
        "receivable": {"books": books, "open_invoices": open_invoices, "difference": books - open_invoices},
        "accounts": {f"{k}: {n}": {"debit": v[0], "credit": v[1]} for (k, n), v in sorted(totals.items())},
    }
