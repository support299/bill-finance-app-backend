"""Splitting one transaction into parts: a check that pays 50 invoices, a deposit holding several clients' payments.

The parts are saved as a whole (replacing any earlier parts) and must add up to the transaction's amount. They can be typed in or
read from an uploaded/pasted statement. Nothing here changes the transaction's amount, so a completed bank reconciliation is not affected.
"""

import csv
import io
from decimal import Decimal

from django.db import transaction as db_transaction
from rest_framework.exceptions import ValidationError

from apps.banking.services.csv_import import _decode, _money, _norm

from ..models import PROJECT_ROLES, Ledger, Project, Transaction, TransactionSplit
from .journal import repost
from .transactions import visible_categories

MAX_PARTS = 500
REFERENCE_HEADERS = (
    "invoice",
    "invoice #",
    "invoice no",
    "invoice number",
    "inv",
    "inv #",
    "reference",
    "ref",
    "document",
    "doc",
    "description",
    "memo",
)
AMOUNT_HEADERS = ("amount", "total", "paid", "payment", "amt", "net", "invoice amount")
PROJECT_HEADERS = ("project", "job", "job name", "project name")
CATEGORY_HEADERS = ("category", "account")
ROLE_HEADERS = ("role", "cost type", "type")


def _check_can_split(tx: Transaction) -> None:
    if tx.removed_at:
        raise ValidationError({"detail": "This transaction was removed."})
    if tx.source == "ghl":
        raise ValidationError({"detail": "GoHighLevel invoices and payments come from the invoice itself and can't be split here."})
    if tx.is_transfer or tx.gaap_type == "transfer":
        raise ValidationError({"detail": "A transfer between your own accounts can't be split."})


def serialize(tx: Transaction) -> dict:
    parts = list(tx.splits.select_related("category", "project", "ledger"))
    total = sum((p.amount for p in parts), Decimal(0))
    return {
        "amount": str(tx.amount),
        "lines": [
            {
                "id": str(p.id),
                "amount": str(p.amount),
                "reference": p.reference,
                "category_id": str(p.category_id) if p.category_id else None,
                "category": p.category.name if p.category else None,
                "project_id": str(p.project_id) if p.project_id else None,
                "project": p.project.name if p.project else None,
                "project_role": p.project_role,
                "ledger_id": str(p.ledger_id) if p.ledger_id else None,
                "ledger": p.ledger.label if p.ledger else None,
            }
            for p in parts
        ],
        "total": str(total),
        "difference": str(tx.amount - total) if parts else "0.00",
    }


def _owned(model, user, pk, what):
    if not pk:
        return None
    obj = model.objects.for_user(user).filter(pk=pk).first()
    if not obj:
        raise ValidationError({"detail": f"{what} not found."})
    return obj


def replace_splits(user, tx: Transaction, lines: list[dict]) -> None:
    """Saves `lines` as the transaction's parts. An empty list removes them. Raises a readable error if they don't add up."""
    _check_can_split(tx)
    if len(lines) > MAX_PARTS:
        raise ValidationError({"detail": f"At most {MAX_PARTS} lines per transaction."})
    if lines and len(lines) < 2:
        raise ValidationError({"detail": "Add at least two lines, or remove the split."})
    categories = visible_categories(user)
    built, total = [], Decimal(0)
    for i, ln in enumerate(lines):
        try:
            amount = Decimal(str(ln.get("amount"))).quantize(Decimal("0.01"))
        except Exception as e:  # noqa: BLE001
            raise ValidationError({"detail": f"Line {i + 1}: the amount is not a number."}) from e
        if amount == 0:
            raise ValidationError({"detail": f"Line {i + 1}: the amount can't be zero."})
        role = ln.get("project_role") or None
        if role and role not in PROJECT_ROLES:
            raise ValidationError({"detail": f"Line {i + 1}: unknown project role."})
        cat_id = ln.get("category_id") or None
        if cat_id and not categories.filter(pk=cat_id).exists():
            raise ValidationError({"detail": f"Line {i + 1}: category not found."})
        project = _owned(Project, user, ln.get("project_id"), "Project")
        ledger = _owned(Ledger, user, ln.get("ledger_id"), "Ledger account")
        total += amount
        built.append(
            TransactionSplit(
                user=user,
                transaction=tx,
                amount=amount,
                reference=(ln.get("reference") or "")[:200],
                category_id=cat_id,
                project=project,
                project_role=role,
                ledger=ledger,
                position=i,
            )
        )
    if built and total != tx.amount:
        diff = tx.amount - total
        raise ValidationError(
            {
                "detail": f"The lines add up to {total:,.2f} but the transaction is {tx.amount:,.2f}. {abs(diff):,.2f} {'left to assign' if diff * tx.amount > 0 else 'too much'}."
            }
        )
    with db_transaction.atomic():
        tx.splits.all().delete()
        TransactionSplit.objects.bulk_create(built)
    repost([tx.id])


def _pick(headers: dict, candidates) -> int | None:
    for c in candidates:
        if c in headers:
            return headers[c]
    return None


def parse_statement(user, tx: Transaction, data: bytes) -> dict:
    """Reads a statement (CSV, or text pasted from a spreadsheet) into proposed lines. Nothing is saved.

    Needs an amount column; reference/invoice, project, category and role columns are optional. Statement amounts are read as the
    statement shows them (an invoice is positive, a credit memo negative) and given the transaction's direction, so a check for
    1,000 can be broken into invoices that add up to 1,000. Names that match no project or category are reported, not guessed.
    """
    _check_can_split(tx)
    text = _decode(data)
    if not text.strip():
        raise ValidationError({"detail": "The file is empty."})
    first = next(line for line in text.splitlines() if line.strip())
    delimiter = max(",;\t|", key=first.count)
    grid = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    headers: dict = {}
    head_at = None
    for i, row in enumerate(grid[:30]):
        names = [_norm(c) for c in row]
        if any(n in AMOUNT_HEADERS for n in names):
            head_at = i
            for j, n in enumerate(names):
                headers.setdefault(n, j)
            break
    if head_at is None:
        raise ValidationError({"detail": "Could not find an Amount column. The first row needs headings such as Invoice, Amount, Project."})
    c_amt, c_ref = _pick(headers, AMOUNT_HEADERS), _pick(headers, REFERENCE_HEADERS)
    c_proj, c_cat, c_role = _pick(headers, PROJECT_HEADERS), _pick(headers, CATEGORY_HEADERS), _pick(headers, ROLE_HEADERS)
    body = grid[head_at + 1 :]
    if len(body) > MAX_PARTS:
        raise ValidationError({"detail": f"Too many lines ({len(body)}). At most {MAX_PARTS} per transaction."})

    projects = {p.name.strip().lower(): p for p in Project.objects.for_user(user)}
    categories = {c.name.strip().lower(): c for c in visible_categories(user)}
    sign = 1 if tx.amount > 0 else -1
    lines, problems = [], []

    def cell(row, idx):
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    for n, row in enumerate(body, start=head_at + 2):
        amount = _money(cell(row, c_amt))
        if amount is None:
            problems.append({"line": n, "reason": "no readable amount"})
            continue
        if amount == 0:
            continue
        line = {
            "amount": str((amount * sign).quantize(Decimal("0.01"))),
            "reference": cell(row, c_ref),
            "project_id": None,
            "category_id": None,
            "project_role": None,
        }
        name = cell(row, c_proj)
        if name:
            proj = projects.get(name.lower())
            if proj:
                line["project_id"] = str(proj.id)
            else:
                problems.append({"line": n, "reason": f"no project called '{name}'"})
        name = cell(row, c_cat)
        if name:
            cat = categories.get(name.lower())
            if cat:
                line["category_id"] = str(cat.id)
            else:
                problems.append({"line": n, "reason": f"no category called '{name}'"})
        role = cell(row, c_role).lower()
        if role in PROJECT_ROLES:
            line["project_role"] = role
        lines.append(line)
    if not lines:
        raise ValidationError({"detail": "No lines found in this statement."})
    total = sum((Decimal(x["amount"]) for x in lines), Decimal(0))
    return {"lines": lines, "problems": problems[:50], "total": str(total), "difference": str(tx.amount - total)}
