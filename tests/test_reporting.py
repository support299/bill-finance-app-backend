import datetime

import pytest

from apps.banking.models import BankAccount, PlaidItem
from apps.core.crypto import encrypt_secret
from apps.ledger.models import Category, Ledger, Transaction

pytestmark = pytest.mark.django_db
D = datetime.date


def cat(name):
    return Category.objects.get(user__isnull=True, name=name)


def tx(user, amount, date, type_, category=None, **kw):
    from apps.ledger.services.gaap import gaap_flags

    return Transaction.objects.create(
        user=user,
        amount=amount,
        transaction_date=date,
        user_category=cat(category) if category else None,
        merchant_name=kw.pop("merchant_name", category),
        **gaap_flags(type_),
        **kw,
    )


def test_cash_flow_report(api_a, user_a, user_b):
    tx(user_a, 1000, D(2026, 1, 10), "revenue", "Sales Revenue")
    tx(user_a, -300, D(2026, 1, 12), "operating_expense", "Software & Technology", merchant_name="AWS")
    tx(user_a, -200, D(2026, 2, 12), "operating_expense", "Software & Technology", merchant_name="AWS")
    tx(user_a, -50, D(2026, 2, 13), "transfer", "Internal Transfer")
    tx(user_b, 99999, D(2026, 1, 10), "revenue", "Sales Revenue")  # other tenant: must not leak in
    r = api_a.get("/api/v1/reports/data/?from=2026-01-01&to=2026-03-31").data
    t = r["totals"]
    assert (t["total_income"], t["total_expenses"], t["net_cash_flow"], t["month_count"]) == (1000, 500, 500, 2)
    assert t["transfer_volume"] == 0  # transfers excluded unless requested
    assert [s["period"] for s in r["series"]] == ["2026-01", "2026-02"]
    assert r["expense_by_category"] == [{"name": "Software & Technology", "color": None, "value": 500.0}]
    assert r["top_merchants"] == [{"name": "AWS", "value": 500.0}]
    assert r["largest_expense"]["amount"] == -300
    with_tr = api_a.get("/api/v1/reports/data/?from=2026-01-01&to=2026-03-31&include_transfers=true").data
    assert with_tr["totals"]["transfer_volume"] == 50
    weekly = api_a.get("/api/v1/reports/data/?from=2026-01-01&to=2026-03-31&granularity=week").data["series"]
    assert weekly[0]["period"] == "2026-01-05"  # Monday
    assert api_a.get("/api/v1/reports/data/?from=2026-03-01&to=2026-01-01").status_code == 400
    assert api_a.get("/api/v1/reports/data/").status_code == 400


def test_dashboard_balance(api_a, user_a):
    item = PlaidItem.objects.create(user=user_a, item_id="i", access_token_encrypted=encrypt_secret("t"))
    BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="chk", account_type="depository", current_balance=1000)
    BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="cc", account_type="credit", current_balance=250)
    r = api_a.get("/api/v1/dashboard/?from=2026-01-01&to=2026-12-31").data
    assert r["current_balance"] == 750 and len(r["accounts"]) == 2
    only = api_a.get("/api/v1/dashboard/?from=2026-01-01&to=2026-12-31&account_ids=chk").data
    assert only["current_balance"] == 1000


def test_accounting_statements(api_a, user_a):
    ar = Ledger.objects.create(user=user_a, label="Customer", account_class="current_asset", contact_name="Cus")
    tx(user_a, 1000, D(2026, 1, 10), "revenue", "Service Revenue", source="manual")
    tx(user_a, -400, D(2026, 1, 11), "cogs", "Cost of Goods Sold", source="manual")
    tx(user_a, -100, D(2026, 1, 12), "operating_expense", "Utilities", source="manual")
    tx(
        user_a,
        700,
        D(2026, 1, 13),
        "asset",
        None,
        ledger=ar,
        source="manual",
        accounting_status="accounts_receivable",
        due_date=D(2026, 1, 20),
    )
    r = api_a.get("/api/v1/reports/accounting/?from=2026-01-01&to=2026-03-31").data
    pl = r["profit_and_loss"]
    assert (pl["total_revenue"], pl["total_cogs"], pl["gross_profit"], pl["total_operating_expenses"], pl["net_profit"]) == (
        1000,
        400,
        600,
        100,
        500,
    )
    assert pl["operating_income"] == 500
    assert r["balance_sheet"]["current_assets"] == [{"id": str(ar.id), "label": "Customer", "balance": 700.0}]
    assert r["balance_sheet"]["equity"][-1]["balance"] == 500
    aging = r["ar_aging"]
    assert aging["rows"][0]["label"] == "Customer" and aging["rows"][0]["buckets"]["61-90"] == 700 and aging["totals"]["total"] == 700
    assert r["ap_aging"]["rows"] == []
    tb = r["trial_balance"]
    assert tb["total_debit"] == 1700 and tb["total_credit"] == 500
    assert r["general_ledger"][0]["label"] == "Customer"
    assert {d["id"] for d in r["account_detail"]} == {"manual"}
    # void/pending transactions are excluded
    tx(user_a, 5000, D(2026, 1, 14), "revenue", "Sales Revenue", status="void")
    assert api_a.get("/api/v1/reports/accounting/?from=2026-01-01&to=2026-03-31").data["profit_and_loss"]["total_revenue"] == 1000


def test_saved_reports_crud(api_a, api_b):
    r = api_a.post(
        "/api/v1/saved-reports/",
        {"name": "Q1", "report_type": "pnl", "date_from": "2026-01-01", "date_to": "2026-03-31", "configuration_json": {"x": 1}},
        format="json",
    )
    assert r.status_code == 201
    assert len(api_a.get("/api/v1/saved-reports/").data["results"]) == 1
    assert api_b.get("/api/v1/saved-reports/").data["results"] == []
    assert api_b.delete(f"/api/v1/saved-reports/{r.data['id']}/").status_code == 404


def test_pl_lines_link_to_transactions(api_a, user_a):
    tx(user_a, 1000, D(2026, 1, 10), "revenue", "Sales Revenue")
    tx(user_a, 400, D(2026, 1, 11), "revenue", None)
    tx(user_a, 700, D(2026, 1, 12), "revenue", "Sales Revenue", status="void")
    rep = api_a.get("/api/v1/reports/accounting/?from=2026-01-01&to=2026-03-31").data["profit_and_loss"]
    lines = {line["name"]: line for line in rep["revenue"]}
    assert lines["Sales Revenue"]["category_id"] and lines["Uncategorized"]["category_id"] is None
    base = "/api/v1/transactions/?from=2026-01-01&to=2026-03-31&gaap_type=revenue&reportable=true"
    got = api_a.get(f"{base}&category_ids={lines['Sales Revenue']['category_id']}").data
    assert got["count"] == 1  # void entry is excluded, like the P&L
    assert api_a.get(f"{base}&uncategorized=true").data["count"] == 1
