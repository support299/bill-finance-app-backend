import datetime
from decimal import Decimal

import pytest

from apps.ledger.models import Category, Project, Transaction
from apps.ledger.services.gaap import gaap_flags

pytestmark = pytest.mark.django_db


def tx(user, amount, type_, **kw):
    return Transaction.objects.create(
        user=user, amount=amount, transaction_date=datetime.date(2026, 3, 5), merchant_name=f"t{amount}", **gaap_flags(type_), **kw
    )


def test_project_pl_roles_and_isolation(api_a, api_b, user_a, user_b):
    pid = api_a.post("/api/v1/projects/", {"name": "Jim Smith – Kitchen", "budget": "4000"}, format="json").data["id"]
    deposit = tx(user_a, 5000, "revenue")
    gear = tx(user_a, -1800, "cogs")
    crew = tx(user_a, -1200, "operating_expense")
    xfer = tx(user_a, -50, "transfer")
    void = tx(user_a, -999, "cogs", status="void")
    other = tx(user_a, -10, "operating_expense")  # not in the project
    r = api_a.post(
        "/api/v1/transactions/bulk-assign-project/",
        {"ids": [str(deposit.id), str(gear.id)], "project_id": pid, "project_role": "equipment"},
        format="json",
    )
    assert r.data["updated"] == 2
    assert api_a.patch(f"/api/v1/transactions/{crew.id}/", {"project_id": pid, "project_role": "labor"}, format="json").status_code == 200
    for t in (xfer, void):
        api_a.patch(f"/api/v1/transactions/{t.id}/", {"project_id": pid}, format="json")
    rep = api_a.get(f"/api/v1/projects/{pid}/report/").data["report"]
    assert (Decimal(rep["revenue"]), Decimal(rep["total_costs"]), Decimal(rep["profit"])) == (5000, 3000, 2000)
    assert (Decimal(rep["costs"]["equipment"]), Decimal(rep["costs"]["labor"])) == (1800, 1200)
    assert rep["ignored_count"] == 1 and rep["budget_used_pct"] == 75.0
    lst = api_a.get("/api/v1/projects/").data["projects"]
    assert Decimal(lst[0]["totals"]["profit"]) == 2000
    assert api_a.get(f"/api/v1/transactions/?project_ids={pid}").data["count"] == 5
    assert api_a.get("/api/v1/transactions/?no_project=true").data["count"] == 1  # `other`
    other.refresh_from_db()
    # P&L can be narrowed to the project
    pl = api_a.get(f"/api/v1/reports/accounting/?from=2026-01-01&to=2026-12-31&project_id={pid}").data["profit_and_loss"]
    assert pl["total_revenue"] == 5000 and pl["net_profit"] == 2000
    # another tenant can neither see nor use it
    assert api_b.get(f"/api/v1/projects/{pid}/report/").status_code == 404
    mine = tx(user_b, 10, "revenue")
    assert api_b.patch(f"/api/v1/transactions/{mine.id}/", {"project_id": pid}, format="json").status_code == 400
    assert (
        api_b.post("/api/v1/transactions/bulk-assign-project/", {"ids": [str(mine.id)], "project_id": pid}, format="json").status_code
        == 400
    )
    # removing a transaction from the project clears its role
    api_a.post("/api/v1/transactions/bulk-assign-project/", {"ids": [str(gear.id)], "project_id": None}, format="json")
    gear.refresh_from_db()
    assert gear.project_id is None and gear.project_role is None
    assert Project.objects.filter(pk=pid).exists() and Category.objects.exists()
