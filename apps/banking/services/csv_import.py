"""Bank-statement CSV import: the fallback for banks and cards Plaid cannot connect to.

Rows become ordinary Transactions (source "csv") on a token-less "csv" connection with one synthetic BankAccount per uploaded
account, so categories, reports and reconciliation treat them like Plaid rows.

Idempotent: each row gets a deterministic id (account + date + amount + description + occurrence number within the file) stored in
plaid_transaction_id, which is unique. Uploading the same period again, or an overlapping one, skips rows that already exist and
never touches ones the user has since edited. Two genuinely identical purchases on one day keep distinct ids via the occurrence number.
"""

import csv
import hashlib
import io
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.ledger.models import Transaction
from apps.ledger.services.categorizer import CategorizerInput, categorize_transaction, resolve_ledger_id
from apps.ledger.services.transactions import load_categories_and_rules

from ..models import BankAccount, PlaidItem

CSV_STATUS = "csv"
MAX_ROWS = 20000

DATE_HEADERS = ("date", "transaction date", "posted date", "posting date", "post date", "trans date", "booking date", "effective date")
DESC_HEADERS = ("description", "details", "memo", "payee", "name", "narrative", "transaction description", "merchant", "original description")
AMOUNT_HEADERS = ("amount", "transaction amount", "amt")
DEBIT_HEADERS = ("debit", "debits", "withdrawal", "withdrawals", "money out", "paid out", "charge")
CREDIT_HEADERS = ("credit", "credits", "deposit", "deposits", "money in", "paid in", "payment")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%m/%d/%Y %H:%M:%S")


class CsvImportError(ValueError):
    pass


@dataclass
class ParsedRow:
    date: date
    amount: Decimal  # account view: negative = money out
    description: str
    line: int


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {"line": n, "reason": str}


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").replace("﻿", "").strip().lower())


def _pick(headers: dict[str, int], candidates: tuple[str, ...]) -> int | None:
    for c in candidates:
        if c in headers:
            return headers[c]
    return None


def _money(raw: str) -> Decimal | None:
    s = (raw or "").strip()
    if not s:
        return None
    neg = "-" in s or ("(" in s and ")" in s)
    s = re.sub(r"[^\d.,]", "", s)
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        # "1,234" is a thousands separator; "12,50" is a decimal comma
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    try:
        v = Decimal(s)
    except InvalidOperation:
        return None
    return -v if neg else v


def _date_format(samples: list[str]) -> str:
    """First format that parses every sample, so 03/04/2026 is read the same way for the whole file."""
    for fmt in DATE_FORMATS:
        try:
            for s in samples:
                datetime.strptime(s, fmt)
            return fmt
        except ValueError:
            continue
    raise CsvImportError("Could not read the dates in this file. Use a format like 2026-03-31 or 03/31/2026.")


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_csv(data: bytes, *, invert_sign: bool = False) -> ParseResult:
    text = _decode(data)
    if not text.strip():
        raise CsvImportError("The file is empty.")
    first = next(l for l in text.splitlines() if l.strip())
    delimiter = max(",;\t|", key=first.count)  # Sniffer is unreliable on free-text descriptions
    grid = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]

    # Banks often put account info above the table: the header is the first row that has a date column and an amount-ish column.
    header_at = None
    for i, row in enumerate(grid[:30]):
        names = {_norm(c) for c in row}
        if names & set(DATE_HEADERS) and names & set(AMOUNT_HEADERS + DEBIT_HEADERS + CREDIT_HEADERS):
            header_at = i
            break
    if header_at is None:
        raise CsvImportError("Could not find the columns. The file needs a Date column and an Amount (or Debit/Credit) column.")

    headers: dict[str, int] = {}
    for i, c in enumerate(grid[header_at]):
        headers.setdefault(_norm(c), i)
    c_date, c_desc = _pick(headers, DATE_HEADERS), _pick(headers, DESC_HEADERS)
    c_amt, c_debit, c_credit = _pick(headers, AMOUNT_HEADERS), _pick(headers, DEBIT_HEADERS), _pick(headers, CREDIT_HEADERS)
    if c_amt is None and c_debit is None and c_credit is None:
        raise CsvImportError("No Amount (or Debit/Credit) column found.")

    body = grid[header_at + 1 :]
    if len(body) > MAX_ROWS:
        raise CsvImportError(f"Too many rows ({len(body)}). Split the file into pieces of {MAX_ROWS} rows or fewer.")

    def cell(row, idx):
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    fmt = _date_format([s for s in (cell(r, c_date) for r in body[:200]) if s][:200] or [""])
    result = ParseResult()
    for n, row in enumerate(body, start=header_at + 2):
        raw_date = cell(row, c_date)
        if not raw_date:
            result.skipped.append({"line": n, "reason": "no date"})
            continue
        try:
            d = datetime.strptime(raw_date, fmt).date()
        except ValueError:
            result.skipped.append({"line": n, "reason": f"unreadable date '{raw_date}'"})
            continue
        if c_amt is not None and cell(row, c_amt):
            amount = _money(cell(row, c_amt))
        else:
            debit, credit = _money(cell(row, c_debit)), _money(cell(row, c_credit))
            if debit is None and credit is None:
                amount = None
            else:
                amount = abs(credit or Decimal(0)) - abs(debit or Decimal(0))
        if amount is None:
            result.skipped.append({"line": n, "reason": "no readable amount"})
            continue
        if invert_sign:
            amount = -amount
        result.rows.append(ParsedRow(d, amount.quantize(Decimal("0.01")), cell(row, c_desc) or "(no description)", n))
    if not result.rows:
        raise CsvImportError("No transactions found in this file.")
    return result


def _row_ids(account: BankAccount, rows: list[ParsedRow]) -> list[str]:
    seen: Counter = Counter()
    ids = []
    for r in rows:
        key = f"{r.date.isoformat()}|{r.amount}|{' '.join(r.description.lower().split())}"
        n = seen[key]
        seen[key] += 1
        digest = hashlib.sha1(f"{key}|{n}".encode()).hexdigest()[:24]
        ids.append(f"csv:{account.id}:{digest}")
    return ids


def get_or_create_account(user, *, account_id=None, institution_name="", account_name="", account_type="depository", mask="") -> BankAccount:
    if account_id:
        acc = BankAccount.objects.for_user(user).select_related("plaid_item").filter(pk=account_id).first()
        if not acc:
            raise CsvImportError("Account not found.")
        if acc.plaid_item.connection_status != CSV_STATUS:
            raise CsvImportError("This account syncs automatically. Upload a CSV only to a CSV account.")
        return acc
    institution_name, account_name = institution_name.strip(), account_name.strip()
    if not institution_name or not account_name:
        raise CsvImportError("Enter the bank name and an account name.")
    item = PlaidItem.objects.for_user(user).filter(connection_status=CSV_STATUS, institution_name=institution_name).first()
    if not item:
        item = PlaidItem.objects.create(
            user=user,
            item_id=f"csv-{uuid.uuid4()}",
            institution_name=institution_name,
            access_token_encrypted="",  # no live connection: sync and webhooks skip it
            environment="csv",
            connection_status=CSV_STATUS,
            initial_sync_done=True,
        )
    return BankAccount.objects.create(
        user=user,
        plaid_item=item,
        plaid_account_id=f"csv-{uuid.uuid4()}",
        institution_id=None,
        account_name=account_name,
        mask=(mask or "").strip()[:10] or None,
        account_type=account_type,
        account_subtype=None,
    )


def import_rows(user, account: BankAccount, parsed: ParseResult, *, commit: bool = True) -> dict:
    ids = _row_ids(account, parsed.rows)
    existing = set(Transaction.objects.filter(plaid_transaction_id__in=ids).values_list("plaid_transaction_id", flat=True))
    new = [(tid, r) for tid, r in zip(ids, parsed.rows) if tid not in existing]
    summary = {
        "total": len(parsed.rows),
        "added": len(new),
        "duplicates": len(parsed.rows) - len(new),
        "skipped": len(parsed.skipped),
        "skipped_rows": parsed.skipped[:20],
        "date_from": min(r.date for r in parsed.rows).isoformat(),
        "date_to": max(r.date for r in parsed.rows).isoformat(),
    }
    if not commit or not new:
        return summary

    categories, rules = load_categories_and_rules(user)
    objs = []
    for tid, r in new:
        inp = CategorizerInput(
            amount=float(-r.amount),  # categorizer uses Plaid's sign: positive = money out
            merchant_name=None,
            description=r.description,
            original_description=r.description,
            account_type=account.account_type,
        )
        res = categorize_transaction(inp, categories, rules)
        objs.append(
            Transaction(
                user=user,
                plaid_item=account.plaid_item,
                plaid_account_id=account.plaid_account_id,
                plaid_transaction_id=tid,
                ledger_id=resolve_ledger_id(inp, rules),
                original_description=r.description,
                description=r.description,
                amount=r.amount,
                currency=account.iso_currency_code or "USD",
                transaction_date=r.date,
                pending=False,
                status="posted",
                source="csv",
                user_category_id=res.category_id,
                category_source=res.source,
                categorization_confidence=res.confidence,
                transaction_direction=res.direction,
                is_income=res.is_income,
                is_expense=res.is_expense,
                is_transfer=res.is_transfer,
                gaap_type=res.type,
            )
        )
    with db_transaction.atomic():
        # ignore_conflicts covers a concurrent identical upload racing past the existence check
        Transaction.objects.bulk_create(objs, batch_size=500, ignore_conflicts=True)
        PlaidItem.objects.filter(pk=account.plaid_item_id).update(last_sync_at=timezone.now())
    return summary
