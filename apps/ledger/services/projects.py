"""Per-project profit & loss: what came in (deposits/revenue) against what it cost (equipment, labor, other)."""

from decimal import Decimal

from apps.ledger.models import Project, Transaction

from .gaap import EXPENSE_TYPES, INCOME_TYPES, row_gaap_type

COST_ROLES = ("equipment", "labor", "other")


def _project_tx(user, project: Project):
    """Rows that count, the same way the financial statements count them (posted, visible, not void)."""
    return (
        Transaction.objects.for_user(user)
        .filter(project=project, removed_at__isnull=True, is_hidden=False, pending=False)
        .exclude(status="void")
        .select_related("user_category")
        .order_by("transaction_date", "created_at")
    )


def _totals(rows) -> dict:
    revenue = Decimal(0)
    costs = {r: Decimal(0) for r in COST_ROLES}
    ignored = 0
    for t in rows:
        g = row_gaap_type(t)
        if g in INCOME_TYPES:
            revenue += t.amount
        elif g in EXPENSE_TYPES:
            costs[t.project_role if t.project_role in COST_ROLES else "other"] += -t.amount
        else:
            ignored += 1  # transfers, assets, liabilities and equity are not project profit
    total_costs = sum(costs.values(), Decimal(0))
    profit = revenue - total_costs
    return {
        "revenue": revenue,
        "costs": costs,
        "total_costs": total_costs,
        "profit": profit,
        "margin_pct": float(profit / revenue * 100) if revenue else None,
        "ignored_count": ignored,
    }


def report(user, project: Project) -> dict:
    rows = list(_project_tx(user, project))
    t = _totals(rows)
    budget = project.budget
    return {
        **t,
        "budget": budget,
        "budget_used_pct": float(t["total_costs"] / budget * 100) if budget else None,
        "transactions": [
            {
                "id": str(x.id),
                "date": x.transaction_date,
                "description": x.merchant_name or x.description or "—",
                "category": x.user_category.name if x.user_category else "Uncategorized",
                "amount": x.amount,
                "type": row_gaap_type(x),
                "role": x.project_role,
            }
            for x in rows
        ],
    }


def summaries(user, projects) -> dict:
    """{project_id: totals} for the list page, in a single query."""
    ids = [p.id for p in projects]
    by_project: dict = {i: [] for i in ids}
    rows = (
        Transaction.objects.for_user(user)
        .filter(project_id__in=ids, removed_at__isnull=True, is_hidden=False, pending=False)
        .exclude(status="void")
    )
    for t in rows:
        by_project[t.project_id].append(t)
    return {str(i): _totals(by_project[i]) for i in ids}
