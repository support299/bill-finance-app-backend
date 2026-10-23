"""1099 vendor reporting logic (port of src/lib/vendor1099.functions.ts)."""

from decimal import Decimal

from apps.banking.models import BankAccount
from apps.ledger.models import Transaction

MISC_BOX_LABELS = {
    "rents": "Box 1 – Rents",
    "royalties": "Box 2 – Royalties",
    "other_income": "Box 3 – Other income",
    "medical": "Box 6 – Medical & health care payments",
    "attorney_proceeds": "Box 10 – Gross proceeds paid to an attorney",
}
TAX_CLASSIFICATION_LABELS = {
    "individual": "Individual / sole proprietor",
    "llc_single": "Single-member LLC (disregarded)",
    "llc_partnership": "LLC taxed as partnership",
    "llc_c_corp": "LLC taxed as C corporation",
    "llc_s_corp": "LLC taxed as S corporation",
    "partnership": "Partnership",
    "c_corp": "C corporation",
    "s_corp": "S corporation",
    "trust_estate": "Trust / estate",
    "other": "Other",
}
CORP = {"c_corp", "s_corp", "llc_c_corp", "llc_s_corp"}


def threshold_1099(year: int, form: str, box: str | None) -> int:
    """Payments after Dec 31, 2025 use $2,000; earlier years $600. Royalties stay at $10."""
    if form == "misc" and box == "royalties":
        return 10
    return 2000 if year >= 2026 else 600


def is_exempt(p) -> bool:
    if p.tax_classification not in CORP:
        return False
    if p.is_attorney:
        return False
    if p.form_type == "misc" and p.misc_box in ("medical", "attorney_proceeds"):
        return False
    return True


def payments_by_ledger(user, year: int, ledger_ids: list) -> dict:
    totals: dict = {}
    if not ledger_ids:
        return totals
    txs = Transaction.objects.for_user(user).filter(
        ledger_id__in=ledger_ids,
        removed_at__isnull=True,
        transaction_date__gte=f"{year}-01-01",
        transaction_date__lte=f"{year}-12-31",
        amount__lt=0,
    )[:20000]
    card_accounts = set(BankAccount.objects.for_user(user).filter(account_type="credit").values_list("plaid_account_id", flat=True))
    for t in txs:
        if t.pending or t.status in ("pending", "void") or t.is_transfer:
            continue
        if t.accounting_status == "accounts_payable":  # unpaid bills are not reportable until paid
            continue
        cur = totals.setdefault(t.ledger_id, {"total": Decimal(0), "excluded_card": Decimal(0), "count": 0})
        v = abs(t.amount)
        if t.plaid_account_id and t.plaid_account_id in card_accounts:
            cur["excluded_card"] += v  # card payments are reported by the processor on 1099-K
        else:
            cur["total"] += v
            cur["count"] += 1
    return totals


def build_row(p, label: str, year: int, pay: dict | None) -> dict:
    total = round(float((pay or {}).get("total", 0)), 2)
    limit = threshold_1099(year, p.form_type, p.misc_box)
    exempt = is_exempt(p)
    issues = []
    if not p.w9_received_at:
        issues.append("W-9 not on file")
    if not p.tin_last4:
        issues.append("TIN missing")
    if not p.legal_name:
        issues.append("Legal name missing")
    if not (p.address_line1 and p.city and p.state and p.postal_code):
        issues.append("Address incomplete")
    if not p.tin_last4 and total > 0:
        issues.append("Backup withholding (24%) may apply")
    is_misc = p.form_type == "misc"
    return {
        "ledger_id": str(p.ledger_id),
        "label": label,
        "legal_name": p.legal_name,
        "form": "1099-MISC" if is_misc else "1099-NEC",
        "box": MISC_BOX_LABELS.get(p.misc_box, "Box 3 – Other income") if is_misc else "Box 1 – Nonemployee compensation",
        "tin_masked": (f"{'**-***' if p.tin_type == 'ein' else '***-**-'}{p.tin_last4}" if p.tin_last4 else None),
        "total": total,
        "excluded_card": round(float((pay or {}).get("excluded_card", 0)), 2),
        "payment_count": (pay or {}).get("count", 0),
        "threshold": limit,
        "exempt": exempt,
        "reportable": (not exempt) and total >= limit,
        "issues": issues,
    }
