"""US GAAP transaction/category types (port of src/lib/gaap.ts)."""

INCOME_TYPES = {"revenue", "other_income"}
EXPENSE_TYPES = {"cogs", "operating_expense", "other_expense"}


def is_income_type(t) -> bool:
    return t in INCOME_TYPES


def is_expense_type(t) -> bool:
    return t in EXPENSE_TYPES


def gaap_flags(type_: str) -> dict:
    """Fields to persist on a transaction for a GAAP type."""
    return {
        "gaap_type": type_,
        "is_income": is_income_type(type_),
        "is_expense": is_expense_type(type_),
        "is_transfer": type_ == "transfer",
    }


def row_gaap_type(tx) -> str:
    """Fallback type for legacy rows without gaap_type."""
    if tx.gaap_type:
        return tx.gaap_type
    if tx.is_income:
        return "revenue"
    if tx.is_expense:
        return "operating_expense"
    if tx.is_transfer:
        return "transfer"
    return "other_expense"


def accounting_statuses_for_type(t) -> list[str]:
    """Receivable/payable statuses that fit a transaction type."""
    if t in ("revenue", "other_income", "asset"):
        return ["accounts_receivable", "received"]
    if t in ("cogs", "operating_expense", "other_expense", "liability"):
        return ["accounts_payable", "paid"]
    return []
