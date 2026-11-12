import re
from decimal import Decimal

from django.db.models import F, OuterRef, Q, Subquery
from django.db.models.functions import Lower
from django.utils import timezone

from apps.ledger.models import Category, CategoryRule, Transaction

from .categorizer import CategorizerInput, categorize_transaction, resolve_ledger_id
from .gaap import gaap_flags


def visible_categories(user):
    return Category.objects.filter(Q(user__isnull=True) | Q(user=user))


def category_positions(user) -> dict:
    """{category id: this user's chosen P&L position}. Categories not in it keep their default order."""
    from apps.ledger.models import CategoryPosition

    return dict(CategoryPosition.objects.for_user(user).values_list("category_id", "sort_order"))


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
    ledger_ids = p.getlist("ledger_ids") if hasattr(p, "getlist") else p.get("ledger_ids")
    if ledger_ids:
        qs = qs.filter(ledger_id__in=ledger_ids)
    if p.get("exclude_settled") in ("true", "1", True):  # open items only: what the balance sheet carries on ledger accounts
        qs = qs.exclude(accounting_status__in=("received", "paid"))
    if p.get("uncategorized") in ("true", "1", True):
        qs = qs.filter(user_category__isnull=True)
    if p.get("receipts") == "missing":  # no receipt or invoice attached yet
        qs = qs.filter(attachments__isnull=True)
    elif p.get("receipts") == "attached":
        qs = qs.filter(attachments__isnull=False).distinct()
    if p.get("unlinked") in ("true", "1", True):  # imported from a bank connection that no longer exists
        qs = qs.filter(source__in=("plaid", "csv"), plaid_item__isnull=True)
    project_ids = p.getlist("project_ids") if hasattr(p, "getlist") else p.get("project_ids")
    if project_ids:
        qs = qs.filter(project_id__in=project_ids)
    if p.get("no_project") in ("true", "1", True):
        qs = qs.filter(project__isnull=True)
    if p.get("gaap_type"):
        qs = qs.filter(gaap_type=p["gaap_type"])
    acc_status = p.getlist("accounting_status") if hasattr(p, "getlist") else p.get("accounting_status")
    if acc_status:  # e.g. the Bills page: accounts_payable (open) or paid
        qs = qs.filter(accounting_status__in=acc_status if isinstance(acc_status, list) else [acc_status])
    if p.get("due_before"):
        qs = qs.filter(due_date__lte=p["due_before"])
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


# sort_by value -> ORDER BY expression. Columns the list shows; names sort case-insensitively.
SORTABLE = {
    "transaction_date": "transaction_date",
    "amount": "amount",
    "merchant_name": Lower("merchant_name"),
    "description": Lower("description"),
    "account": Lower("account_name"),  # annotated below: the bank account's display name
    "gaap_type": "gaap_type",
    "category": Lower("user_category__name"),
    "status": "status",
    "due_date": "due_date",
}


def apply_sort(qs, p):
    field = p.get("sort_by", "transaction_date")
    if field not in SORTABLE:
        field = "transaction_date"
    if field == "account":
        from apps.banking.models import BankAccount

        qs = qs.annotate(
            account_name=Subquery(BankAccount.objects.filter(plaid_account_id=OuterRef("plaid_account_id")).values("account_name")[:1])
        )
    expr = SORTABLE[field]
    desc = p.get("sort_dir") != "asc"
    if isinstance(expr, str):
        primary = F(expr).desc(nulls_last=True) if desc else F(expr).asc(nulls_last=True)
    else:
        primary = expr.desc(nulls_last=True) if desc else expr.asc(nulls_last=True)
    return qs.order_by(primary, "-transaction_date", "-created_at")


def recategorize_all(user) -> int:
    """Re-run the categorizer across the user's non-removed, non-manual transactions, then re-pair transfers."""
    from .transfers import pair_transfers

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
    pair_transfers(user)
    return updated


def rule_match_queryset(user, rule):
    """Existing transactions a non-regex rule would be back-filled onto (None for regex rules, which are not back-filled)."""
    if rule.match_type == "regex":
        return None
    term = re.sub(r"[%,()]", " ", rule.keyword).strip()
    field_lookup = {"starts_with": "istartswith", "exact": "iexact"}.get(rule.match_type, "icontains")
    q = Q()
    for f in ("merchant_name", "description", "original_description"):
        q |= Q(**{f"{f}__{field_lookup}": term})
    return Transaction.objects.for_user(user).filter(removed_at__isnull=True).filter(q)


def preview_rule(user, rule, category=None, sample: int = 5) -> dict:
    """What creating this rule would change right now: how many transactions match, how many would actually change."""
    qs = rule_match_queryset(user, rule)
    if qs is None:
        return {"previewable": False, "matching": 0, "would_change": 0, "sample": []}
    ids = list(qs.values_list("id", flat=True)[:2000])
    base = Transaction.objects.filter(id__in=ids)
    changing = base.exclude(user_category=category) if category else base
    rows = changing.order_by("-transaction_date")[:sample]
    return {
        "previewable": True,
        "matching": len(ids),
        "would_change": changing.count(),
        "sample": [
            {
                "id": str(t.id),
                "date": t.transaction_date,
                "description": t.merchant_name or t.description or "—",
                "amount": t.amount,
                "current_category": t.user_category.name if t.user_category_id else None,
            }
            for t in rows.select_related("user_category")
        ],
    }


def apply_rule_to_existing(user, rule, category) -> int:
    """Back-fill a newly created rule onto existing transactions (non-regex rules only)."""
    qs = rule_match_queryset(user, rule)
    if qs is None:
        return 0
    ids = list(qs.values_list("id", flat=True)[:2000])
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
