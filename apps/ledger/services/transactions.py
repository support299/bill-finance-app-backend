import re
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.ledger.models import Category, CategoryRule, Transaction

from .categorizer import CategorizerInput, categorize_transaction, resolve_ledger_id
from .gaap import gaap_flags


def visible_categories(user):
    return Category.objects.filter(Q(user__isnull=True) | Q(user=user))


def load_categories_and_rules(user):
    return list(visible_categories(user)), list(CategoryRule.objects.for_user(user))


def apply_filters(qs, p):
    """Query-param filters for the transactions list/export (port of applyFilters)."""
    qs = qs.filter(removed_at__isnull=True)
    if p.get("include_hidden") not in ("true", "1", True):
        qs = qs.filter(is_hidden=False)
    if p.get("from"):
        qs = qs.filter(transaction_date__gte=p["from"])
    if p.get("to"):
        qs = qs.filter(transaction_date__lte=p["to"])
    account_ids = p.getlist("account_ids") if hasattr(p, "getlist") else p.get("account_ids")
    if account_ids:
        qs = qs.filter(plaid_account_id__in=account_ids)
    category_ids = p.getlist("category_ids") if hasattr(p, "getlist") else p.get("category_ids")
    if category_ids:
        qs = qs.filter(user_category_id__in=category_ids)
    if p.get("uncategorized") in ("true", "1", True):
        qs = qs.filter(user_category__isnull=True)
    if p.get("unlinked") in ("true", "1", True):  # imported from a bank connection that no longer exists
        qs = qs.filter(source="plaid", plaid_item__isnull=True)
    project_ids = p.getlist("project_ids") if hasattr(p, "getlist") else p.get("project_ids")
    if project_ids:
        qs = qs.filter(project_id__in=project_ids)
    if p.get("no_project") in ("true", "1", True):
        qs = qs.filter(project__isnull=True)
    if p.get("gaap_type"):
        qs = qs.filter(gaap_type=p["gaap_type"])
    if p.get("reportable") in ("true", "1", True):  # same rows the financial statements count
        qs = qs.exclude(status__in=("void", "pending")).filter(pending=False)
    direction = p.get("direction", "all")
    if direction == "income":
        qs = qs.filter(is_income=True)
    elif direction == "expense":
        qs = qs.filter(is_expense=True)
    elif direction == "transfer":
        qs = qs.filter(is_transfer=True)
    status = p.get("status", "all")
    if status == "pending":
        qs = qs.filter(pending=True)
    elif status == "posted":
        qs = qs.filter(pending=False)
    if p.get("min_amount") not in (None, ""):
        qs = qs.filter(amount__gte=Decimal(str(p["min_amount"])))
    if p.get("max_amount") not in (None, ""):
        qs = qs.filter(amount__lte=Decimal(str(p["max_amount"])))
    term = re.sub(r"[%,()]", " ", p.get("search", "") or "").strip()
    if term:
        qs = qs.filter(Q(merchant_name__icontains=term) | Q(description__icontains=term) | Q(original_description__icontains=term))
    return qs


SORTABLE = {"transaction_date", "amount", "merchant_name"}


def apply_sort(qs, p):
    field = p.get("sort_by", "transaction_date")
    if field not in SORTABLE:
        field = "transaction_date"
    prefix = "" if p.get("sort_dir") == "asc" else "-"
    return qs.order_by(f"{prefix}{field}", "-created_at")


def recategorize_all(user) -> int:
    """Re-run the categorizer across the user's non-removed, non-manual transactions."""
    categories, rules = load_categories_and_rules(user)
    updated = 0
    qs = Transaction.objects.for_user(user).filter(removed_at__isnull=True).exclude(source="manual")[:5000]
    for tx in qs:
        inp = CategorizerInput(
            merchant_name=tx.merchant_name,
            description=tx.description,
            original_description=tx.original_description,
            plaid_primary=tx.plaid_personal_finance_primary_category,
            plaid_detailed=tx.plaid_personal_finance_detailed_category,
            amount=-float(tx.amount),
            payment_channel=tx.payment_channel,
            transaction_type=tx.transaction_type,
        )
        result = categorize_transaction(inp, categories, rules)
        ledger_id = resolve_ledger_id(inp, rules)
        tx.user_category_id = result.category_id
        tx.category_source = result.source
        tx.categorization_confidence = result.confidence
        tx.transaction_direction = result.direction
        tx.is_income, tx.is_expense, tx.is_transfer = result.is_income, result.is_expense, result.is_transfer
        tx.gaap_type = result.type
        if ledger_id:
            tx.ledger_id = ledger_id
        tx.updated_at = timezone.now()
        tx.save(
            update_fields=[
                "user_category",
                "category_source",
                "categorization_confidence",
                "transaction_direction",
                "is_income",
                "is_expense",
                "is_transfer",
                "gaap_type",
                "ledger",
                "updated_at",
            ]
        )
        updated += 1
    return updated


def apply_rule_to_existing(user, rule, category) -> int:
    """Back-fill a newly created rule onto existing transactions (non-regex rules only)."""
    if rule.match_type == "regex":
        return 0
    term = re.sub(r"[%,()]", " ", rule.keyword).strip()
    field_lookup = {"starts_with": "istartswith", "exact": "iexact"}.get(rule.match_type, "icontains")
    q = Q()
    for f in ("merchant_name", "description", "original_description"):
        q |= Q(**{f"{f}__{field_lookup}": term})
    ids = list(Transaction.objects.for_user(user).filter(removed_at__isnull=True).filter(q).values_list("id", flat=True)[:2000])
    if not ids:
        return 0
    patch = {}
    if category:
        patch.update(
            user_category=category,
            category_source="user_rule" if rule.source == "user" else "learned_rule",
            categorization_confidence="high",
            **gaap_flags(category.type),
        )
    if rule.ledger_id:
        patch["ledger_id"] = rule.ledger_id
    Transaction.objects.filter(id__in=ids).update(**patch)
    return len(ids)
