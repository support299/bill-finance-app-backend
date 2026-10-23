"""Deterministic, layered transaction categorizer (port of src/lib/categorizer.ts).

Priority: user rules > learned rules > Plaid category > keyword > income/expense heuristic > fallback.
"""

import re
from dataclasses import dataclass

from .gaap import is_expense_type, is_income_type

PLAID_PRIMARY_MAP = {
    "INCOME": "Other Income",
    "TRANSFER_IN": "Internal Transfer",
    "TRANSFER_OUT": "Internal Transfer",
    "LOAN_PAYMENTS": "Loan Principal Repayment",
    "BANK_FEES": "Bank & Service Charges",
    "ENTERTAINMENT": "Meals & Entertainment",
    "FOOD_AND_DRINK": "Meals & Entertainment",
    "GENERAL_MERCHANDISE": "Office Supplies & Expenses",
    "HOME_IMPROVEMENT": "Repairs & Maintenance",
    "MEDICAL": "Employee Benefits",
    "PERSONAL_CARE": "Owner Draw",
    "GENERAL_SERVICES": "Professional Fees",
    "GOVERNMENT_AND_NON_PROFIT": "Taxes & Licenses",
    "TRANSPORTATION": "Auto & Vehicle",
    "TRAVEL": "Travel",
    "RENT_AND_UTILITIES": "Utilities",
}

PLAID_DETAILED_MAP = {
    "INCOME_WAGES": "Other Income",
    "INCOME_DIVIDENDS": "Dividend Income",
    "INCOME_INTEREST_EARNED": "Interest Income",
    "INCOME_TAX_REFUND": "Other Income",
    "INCOME_OTHER_INCOME": "Other Income",
    "TRANSFER_IN_INVESTMENT": "Investment Purchase",
    "TRANSFER_OUT_INVESTMENT": "Investment Purchase",
    "TRANSFER_IN_LOANS": "Loan Proceeds",
    "TRANSFER_IN_ACCOUNT_TRANSFER": "Internal Transfer",
    "TRANSFER_OUT_ACCOUNT_TRANSFER": "Internal Transfer",
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT": "Credit Card Payment",
    "LOAN_PAYMENTS_MORTGAGE_PAYMENT": "Loan Principal Repayment",
    "LOAN_PAYMENTS_CAR_PAYMENT": "Loan Principal Repayment",
    "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT": "Owner Draw",
    "BANK_FEES_INTEREST_CHARGE": "Interest Expense",
    "FOOD_AND_DRINK_GROCERIES": "Other Business Expenses",
    "TRANSPORTATION_GAS": "Auto & Vehicle",
    "RENT_AND_UTILITIES_RENT": "Rent or Lease",
    "RENT_AND_UTILITIES_INTERNET_AND_CABLE": "Utilities",
    "RENT_AND_UTILITIES_TELEPHONE": "Utilities",
    "ENTERTAINMENT_STREAMING": "Dues & Subscriptions",
    "GENERAL_SERVICES_INSURANCE": "Insurance",
    "GENERAL_SERVICES_EDUCATION": "Education & Training",
    "GENERAL_SERVICES_ACCOUNTING_AND_FINANCIAL_PLANNING": "Professional Fees",
    "GENERAL_SERVICES_POSTAGE_AND_SHIPPING": "Freight & Shipping",
    "GENERAL_MERCHANDISE_OFFICE_SUPPLIES": "Office Supplies & Expenses",
    "GENERAL_MERCHANDISE_ELECTRONICS": "Software & Technology",
    "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT": "Income Tax Expense",
}

_KEYWORDS = [
    (r"\b(payroll|adp|gusto|paychex)\b", "Salaries & Wages"),
    (r"\b(interest (credit|paid|earned))\b", "Interest Income"),
    (r"\b(interest charge|finance charge)\b", "Interest Expense"),
    (r"\b(dividend)\b", "Dividend Income"),
    (r"\b(refund|reversal|return)\b", "Sales Returns & Allowances"),
    (r"\b(stripe|square|shopify payout|paypal transfer)\b", "Sales Revenue"),
    (r"\b(upwork|fiverr|invoice|client payment)\b", "Service Revenue"),
    (r"\b(rent received|tenant)\b", "Rental Revenue"),
    (r"(google ads|facebook ads|meta ads|linkedin ads|mailchimp|advertis)", "Advertising & Marketing"),
    (r"(adobe|dropbox|notion|openai|github|aws|google workspace|microsoft|slack|zoom|atlassian)", "Software & Technology"),
    (r"(netflix|spotify|hulu|disney\+|apple\.com\/bill|prime video)", "Dues & Subscriptions"),
    (r"(uber|lyft|metro|transit|taxi|parking|toll)", "Travel"),
    (r"(shell|chevron|exxon|bp |gas station|texaco|76 |jiffy lube)", "Auto & Vehicle"),
    (r"(starbucks|dunkin|mcdonald|chipotle|restaurant|pizza|cafe|coffee|doordash|grubhub|ubereats)", "Meals & Entertainment"),
    (r"(staples|office depot|officemax)", "Office Supplies & Expenses"),
    (r"(amazon|target|ebay|etsy|best buy|ikea|walmart|costco)", "Office Supplies & Expenses"),
    (r"(ups|fedex|usps|dhl|shipstation)", "Freight & Shipping"),
    (r"(cpa|attorney|law firm|legal|accounting|bookkeep|consult)", "Professional Fees"),
    (r"(geico|allstate|state farm|progressive|insurance)", "Insurance"),
    (r"(comcast|xfinity|at&t|verizon|t-mobile|pg&e|electric|water util|utility)", "Utilities"),
    (r"(delta|united air|american air|southwest|airbnb|expedia|marriott|hilton|hotel|amtrak)", "Travel"),
    (r"(tuition|coursera|udemy|seminar|conference)", "Education & Training"),
    (r"(irs|eftps|tax pmt|franchise tax|state tax)", "Taxes & Licenses"),
    (r"(overdraft|service charge|atm fee|monthly fee|nsf|wire fee)", "Bank & Service Charges"),
    (r"(rent payment|lease|property mgmt)", "Rent or Lease"),
    (r"(repair|maintenance|plumb|hvac|janitorial)", "Repairs & Maintenance"),
    (r"(credit card (payment|pmt)|card payment|autopay .*card|chase card|amex epayment)", "Credit Card Payment"),
    (r"(mortgage|loan pmt|loan payment)", "Loan Principal Repayment"),
    (r"(owner draw|distribution)", "Owner Draw"),
    (r"(owner contribution|capital contribution)", "Owner Contribution"),
    (r"(transfer to|transfer from|xfer|online transfer|zelle|venmo|wire transfer)", "Internal Transfer"),
    (r"(vanguard|fidelity|schwab|robinhood|coinbase|betterment|wealthfront)", "Investment Purchase"),
]
KEYWORD_MAP = [(re.compile(p, re.IGNORECASE), name) for p, name in _KEYWORDS]


@dataclass
class CategorizerInput:
    amount: float  # Plaid convention: positive = money leaving the account
    merchant_name: str | None = None
    description: str | None = None
    original_description: str | None = None
    plaid_primary: str | None = None
    plaid_detailed: str | None = None
    payment_channel: str | None = None
    transaction_type: str | None = None
    account_type: str | None = None


@dataclass
class CategorizerResult:
    category_id: str | None
    category_name: str
    type: str
    is_income: bool
    is_expense: bool
    is_transfer: bool
    direction: str
    confidence: str
    source: str


def _haystack(inp: CategorizerInput) -> str:
    return " | ".join(p for p in (inp.merchant_name, inp.description, inp.original_description) if p)


def match_rule(rule, text: str) -> bool:
    """rule needs .keyword and .match_type."""
    t = text.lower()
    k = (rule.keyword or "").lower().strip()
    if not k:
        return False
    mt = rule.match_type
    if mt == "contains":
        return k in t
    if mt == "starts_with":
        return t.lstrip().startswith(k)
    if mt == "exact":
        return t.strip() == k
    if mt == "regex":
        try:
            return re.search(rule.keyword, text, re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def _finalize(category, inp: CategorizerInput, source: str, confidence: str) -> CategorizerResult:
    direction = "inflow" if inp.amount < 0 else "outflow"
    type_ = category.type if category else ("other_income" if direction == "inflow" else "other_expense")
    return CategorizerResult(
        category_id=str(category.id) if category else None,
        category_name=category.name if category else "Uncategorized",
        type=type_,
        is_income=is_income_type(type_),
        is_expense=is_expense_type(type_),
        is_transfer=type_ == "transfer",
        direction=direction,
        confidence=confidence,
        source=source,
    )


def categorize_transaction(inp: CategorizerInput, categories, rules) -> CategorizerResult:
    """categories: objects with id/name/type. rules: objects with keyword/match_type/category_id/priority/active/source."""
    by_id = {str(c.id): c for c in categories}
    by_name = {c.name.lower(): c for c in categories}
    text = _haystack(inp)

    active = sorted((r for r in rules if r.active), key=lambda r: r.priority)
    for group, label in (("user", "user_rule"), ("learned", "learned_rule")):
        for rule in (r for r in active if r.source == group):
            if rule.category_id and match_rule(rule, text):
                return _finalize(by_id.get(str(rule.category_id)), inp, label, "high")

    detailed = PLAID_DETAILED_MAP.get(inp.plaid_detailed) if inp.plaid_detailed else None
    primary = PLAID_PRIMARY_MAP.get(inp.plaid_primary) if inp.plaid_primary else None
    plaid_name = detailed or primary
    if plaid_name and plaid_name.lower() in by_name:
        return _finalize(by_name[plaid_name.lower()], inp, "plaid", "high" if detailed else "medium")

    for pattern, name in KEYWORD_MAP:
        if pattern.search(text) and name.lower() in by_name:
            return _finalize(by_name[name.lower()], inp, "keyword", "medium")

    if inp.amount < 0:
        return _finalize(by_name.get("other income"), inp, "heuristic", "low")
    fallback = by_name.get("other business expenses")
    if fallback:
        return _finalize(fallback, inp, "heuristic", "low")
    return _finalize(by_name.get("uncategorized"), inp, "fallback", "low")


def resolve_ledger_id(inp: CategorizerInput, rules) -> str | None:
    """Ledger account a transaction should be filed under, from active rules."""
    text = _haystack(inp)
    candidates = sorted((r for r in rules if r.active and r.ledger_id), key=lambda r: r.priority)
    for rule in candidates:
        if match_rule(rule, text):
            return str(rule.ledger_id)
    return None


def clean_merchant_keyword(merchant: str | None = None, description: str | None = None) -> str:
    base = (merchant or description or "").lower()
    cleaned = re.sub(r"[^a-z0-9 &.]", " ", base)
    cleaned = re.sub(r"\b(purchase|pos|debit|credit|payment|card|xxxx?\d*|\d{2,})\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return " ".join(cleaned.split(" ")[:2]) or base.strip()
