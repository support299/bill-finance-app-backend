import datetime
from decimal import Decimal

import pytest

from apps.ledger.models import Category, Transaction
from apps.ledger.services.categorizer import CategorizerInput, categorize_transaction, resolve_ledger_id
from apps.ledger.services.gaap import accounting_statuses_for_type, gaap_flags
from apps.ledger.services.vendor1099 import threshold_1099

pytestmark = pytest.mark.django_db


def cat(name):
    return Category.objects.get(user__isnull=True, name=name)


def test_seeded_chart_of_accounts():
    assert Category.objects.filter(user__isnull=True, is_system=True).count() == 47
    assert cat("Sales Revenue").type == "revenue" and cat("Owner Draw").type == "equity"


def test_gaap_helpers():
    assert gaap_flags("revenue") == {"gaap_type": "revenue", "is_income": True, "is_expense": False, "is_transfer": False}
    assert gaap_flags("cogs")["is_expense"] and gaap_flags("transfer")["is_transfer"]
    assert accounting_statuses_for_type("revenue") == ["accounts_receivable", "received"]
    assert accounting_statuses_for_type("transfer") == []


class R:  # minimal rule stand-in
    def __init__(self, **kw):
        self.__dict__.update(dict(id="r", ledger_id=None, priority=100, active=True, source="user", match_type="contains") | kw)


def run(inp, rules=()):
    return categorize_transaction(inp, list(Category.objects.filter(user__isnull=True)), list(rules))


def test_categorizer_layers():
    # keyword layer
    r = run(CategorizerInput(amount=12.5, merchant_name="Starbucks", description="STARBUCKS #123"))
    assert (r.category_name, r.source, r.is_expense) == ("Meals & Entertainment", "keyword", True)
    # plaid detailed beats keyword and is high confidence
    r = run(CategorizerInput(amount=50, merchant_name="x", plaid_primary="TRANSPORTATION", plaid_detailed="TRANSPORTATION_GAS"))
    assert (r.category_name, r.source, r.confidence) == ("Auto & Vehicle", "plaid", "high")
    # user rule beats everything
    sw = cat("Software & Technology")
    r = run(CategorizerInput(amount=9, merchant_name="Starbucks"), [R(keyword="starbucks", category_id=sw.id)])
    assert (r.category_name, r.source) == ("Software & Technology", "user_rule")
    # heuristics: unknown inflow vs outflow
    r = run(CategorizerInput(amount=-100, merchant_name="zzzq"))
    assert (r.category_name, r.is_income, r.direction) == ("Other Income", True, "inflow")
    r = run(CategorizerInput(amount=100, merchant_name="zzzq"))
    assert (r.category_name, r.is_expense, r.direction) == ("Other Business Expenses", True, "outflow")


def test_rule_match_types_and_ledger_resolution():
    inp = CategorizerInput(amount=5, merchant_name="Acme Corp", description="Acme Corp invoice 42")
    assert resolve_ledger_id(inp, [R(keyword="acme", ledger_id="L1")]) == "L1"
    assert resolve_ledger_id(inp, [R(keyword="corp", match_type="exact", ledger_id="L1")]) is None
    assert resolve_ledger_id(inp, [R(keyword=r"invoice \d+", match_type="regex", ledger_id="L2")]) == "L2"
    assert resolve_ledger_id(inp, [R(keyword="(", match_type="regex", ledger_id="L3")]) is None  # invalid regex never raises
    assert resolve_ledger_id(inp, [R(keyword="acme", ledger_id="L1", active=False)]) is None


def test_1099_thresholds():
    assert threshold_1099(2025, "nec", None) == 600
    assert threshold_1099(2026, "nec", None) == 2000
    assert threshold_1099(2026, "misc", "royalties") == 10


def mk_ledger(api, label="Acme", cls="current_asset"):
    r = api.post("/api/v1/ledgers/", {"label": label, "account_class": cls}, format="json")
    assert r.status_code == 201, r.data
    return r.data["id"]


def test_ledger_crud_unique_label_and_tags(api_a):
    lid = mk_ledger(api_a)
    assert api_a.post("/api/v1/ledgers/", {"label": "Acme", "account_class": "equity"}, format="json").status_code == 400
    assert api_a.post(f"/api/v1/ledgers/{lid}/tags/", {"name": "VIP"}, format="json").status_code == 200
    assert api_a.post(f"/api/v1/ledgers/{lid}/tags/", {"name": "vip"}, format="json").status_code == 200  # case-insensitive reuse
    tags = api_a.get("/api/v1/tags/").data
    assert len(tags) == 1
    detail = api_a.get(f"/api/v1/ledgers/{lid}/").data
    assert detail["tags"][0]["name"] == "VIP" and detail["transaction_count"] == 0
    assert api_a.delete(f"/api/v1/ledgers/{lid}/tags/{tags[0]['id']}/").status_code == 204
    assert api_a.delete(f"/api/v1/ledgers/{lid}/").status_code == 204


def test_manual_entries_and_accounting_status_rules(api_a):
    lid = mk_ledger(api_a)
    body = {
        "ledger_id": lid,
        "date": "2026-03-01",
        "description": "Consulting",
        "amount": "-250.00",
        "type": "revenue",
        "accounting_status": "accounts_receivable",
    }
    r = api_a.post("/api/v1/entries/", body, format="json")
    assert r.status_code == 201
    tx = Transaction.objects.get(pk=r.data["id"])
    assert tx.amount == 250 and tx.is_income and tx.source == "manual"  # revenue stored positive
    # expense stored negative
    r2 = api_a.post(
        "/api/v1/entries/", {**body, "type": "operating_expense", "amount": "40", "accounting_status": "accounts_payable"}, format="json"
    )
    assert Transaction.objects.get(pk=r2.data["id"]).amount == -40
    # status must match the type
    bad = api_a.post("/api/v1/entries/", {**body, "accounting_status": "paid"}, format="json")
    assert bad.status_code == 400
    # only manual entries are deletable through /entries
    plaid_tx = Transaction.objects.create(user=tx.user, amount=-1, transaction_date=datetime.date(2026, 1, 1), source="plaid")
    assert api_a.delete(f"/api/v1/entries/{plaid_tx.id}/").status_code == 404
    assert api_a.delete(f"/api/v1/entries/{tx.id}/").status_code == 204


def test_changing_ledger_class_clears_incompatible_statuses(api_a):
    lid = mk_ledger(api_a)
    r = api_a.post(
        "/api/v1/entries/",
        {
            "ledger_id": lid,
            "date": "2026-03-01",
            "description": "x",
            "amount": "10",
            "type": "revenue",
            "accounting_status": "accounts_receivable",
        },
        format="json",
    )
    api_a.patch(f"/api/v1/ledgers/{lid}/", {"label": "Acme", "account_class": "current_liability"}, format="json")
    assert Transaction.objects.get(pk=r.data["id"]).accounting_status is None


def seed_txs(user):
    d = datetime.date
    rows = [
        ("Starbucks", -12.5, d(2026, 2, 1), False, True),
        ("Stripe payout", 900, d(2026, 2, 3), True, False),
        ("Hidden thing", -5, d(2026, 2, 4), False, True),
    ]
    out = []
    for name, amt, dt, inc, exp in rows:
        out.append(
            Transaction.objects.create(
                user=user, merchant_name=name, description=name, amount=amt, transaction_date=dt, is_income=inc, is_expense=exp
            )
        )
    out[2].is_hidden = True
    out[2].save()
    return out


def test_transaction_list_filters_sort_pagination(api_a, user_a):
    seed_txs(user_a)
    r = api_a.get("/api/v1/transactions/")
    assert r.data["count"] == 2  # hidden excluded by default
    assert api_a.get("/api/v1/transactions/?include_hidden=true").data["count"] == 3
    assert api_a.get("/api/v1/transactions/?direction=income").data["count"] == 1
    assert api_a.get("/api/v1/transactions/?search=starb").data["count"] == 1
    assert api_a.get("/api/v1/transactions/?min_amount=0").data["count"] == 1
    assert api_a.get("/api/v1/transactions/?from=2026-02-02&to=2026-02-10").data["count"] == 1
    first = api_a.get("/api/v1/transactions/?sort_by=amount&sort_dir=asc").data["results"][0]
    assert first["merchant_name"] == "Starbucks"
    assert api_a.get("/api/v1/transactions/?page_size=1").data["results"].__len__() == 1
    assert "categories" in first and "user_category_id" in first


def test_transaction_list_sorts_by_every_visible_column(api_a, user_a):
    from apps.banking.models import BankAccount, PlaidItem

    item = PlaidItem.objects.create(user=user_a, item_id="it-1", access_token_encrypted="x")
    BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="acc-z", account_name="Zeta Checking")
    BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="acc-a", account_name="Alpha Savings")
    meals, rent = cat("Meals & Entertainment"), cat("Rent or Lease")

    def mk(name, **kw):
        base = dict(user=user_a, merchant_name=name, description=name, amount=-5, transaction_date=datetime.date(2026, 2, 1))
        return Transaction.objects.create(**{**base, **kw})

    mk("bravo", description="second", plaid_account_id="acc-z", gaap_type="operating_expense", user_category=rent, status="posted")
    mk("Alpha", description="first", plaid_account_id="acc-a", gaap_type="cost_of_goods_sold", user_category=meals, status="pending")
    mk("charlie", description="third", plaid_account_id=None, gaap_type="other_income", user_category=None, status="void")

    def first(q):
        return api_a.get(f"/api/v1/transactions/?{q}").data["results"][0]["merchant_name"]

    assert first("sort_by=merchant_name&sort_dir=asc") == "Alpha"  # case-insensitive
    assert first("sort_by=merchant_name&sort_dir=desc") == "charlie"
    assert first("sort_by=description&sort_dir=asc") == "Alpha"
    assert first("sort_by=account&sort_dir=asc") == "Alpha"  # Alpha Savings < Zeta Checking, no account last
    assert first("sort_by=account&sort_dir=desc") == "bravo"
    assert first("sort_by=gaap_type&sort_dir=asc") == "Alpha"  # cost_of_goods_sold < operating_expense < other_income
    assert first("sort_by=category&sort_dir=asc") == "Alpha"  # Meals < Rent, uncategorized last
    assert first("sort_by=category&sort_dir=desc") == "bravo"
    assert first("sort_by=status&sort_dir=asc") == "Alpha"  # pending < posted < void
    assert first("sort_by=nope") in ("Alpha", "bravo", "charlie")  # unknown key falls back to date, no 500


def test_transactions_can_be_listed_by_accounting_status_and_due_date(api_a, user_a):
    def bill(desc, due, status="accounts_payable"):
        return Transaction.objects.create(
            user=user_a,
            description=desc,
            amount=-50,
            transaction_date=datetime.date(2026, 2, 1),
            due_date=due,
            accounting_status=status,
            source="manual",
        )

    bill("later", datetime.date(2026, 3, 20))
    bill("soon", datetime.date(2026, 3, 5))
    bill("paid", datetime.date(2026, 2, 10), status="paid")
    Transaction.objects.create(user=user_a, description="plain", amount=-5, transaction_date=datetime.date(2026, 2, 1))
    r = api_a.get("/api/v1/transactions/?accounting_status=accounts_payable&sort_by=due_date&sort_dir=asc").data
    assert [t["description"] for t in r["results"]] == ["soon", "later"]
    assert api_a.get("/api/v1/transactions/?accounting_status=paid").data["count"] == 1
    assert api_a.get("/api/v1/transactions/?accounting_status=accounts_payable&accounting_status=paid").data["count"] == 3
    assert api_a.get("/api/v1/transactions/?accounting_status=accounts_payable&due_before=2026-03-10").data["count"] == 1


def test_transaction_patch_and_bulk_and_rule_backfill(api_a, user_a):
    a, b, _ = seed_txs(user_a)
    cid = str(cat("Meals & Entertainment").id)
    assert api_a.patch(f"/api/v1/transactions/{a.id}/", {"category_id": cid}, format="json").status_code == 200
    a.refresh_from_db()
    assert a.user_category_id and str(a.user_category_id) == cid and a.is_expense and a.category_source == "user_rule"
    api_a.patch(f"/api/v1/transactions/{a.id}/", {"classification": "revenue"}, format="json")
    a.refresh_from_db()
    assert a.is_income and not a.is_expense
    r = api_a.post("/api/v1/transactions/bulk-categorize/", {"ids": [str(a.id), str(b.id)], "category_id": cid}, format="json")
    assert r.data["updated"] == 2
    # creating a rule back-fills matching existing transactions
    r = api_a.post("/api/v1/rules/", {"keyword": "stripe", "category_id": str(cat("Sales Revenue").id)}, format="json")
    assert r.status_code == 201 and r.data["applied"] == 1
    b.refresh_from_db()
    assert b.is_income and b.category_source == "user_rule"
    # rule needs a category or ledger
    assert api_a.post("/api/v1/rules/", {"keyword": "nothing"}, format="json").status_code == 400
    # list + update + delete rule
    rid = api_a.get("/api/v1/rules/").data[0]["id"]
    assert api_a.patch(f"/api/v1/rules/{rid}/", {"active": False}, format="json").status_code == 200
    assert api_a.delete(f"/api/v1/rules/{rid}/").status_code == 204


def test_recategorize_skips_manual_entries(api_a, user_a):
    a, _, _ = seed_txs(user_a)
    manual = Transaction.objects.create(
        user=user_a, amount=100, transaction_date=datetime.date(2026, 2, 5), source="manual", gaap_type="asset"
    )
    assert api_a.post("/api/v1/transactions/recategorize/", format="json").data["updated"] >= 2
    manual.refresh_from_db()
    assert manual.gaap_type == "asset"
    a.refresh_from_db()
    assert a.user_category.name == "Meals & Entertainment" and a.category_source == "keyword"


def test_export_csv(api_a, user_a):
    seed_txs(user_a)
    r = api_a.get("/api/v1/transactions/export/?output=csv")
    assert r["Content-Type"].startswith("text/csv") and "Starbucks" in r.content.decode()


def test_ledger_summaries_and_assign(api_a, user_a):
    lid = mk_ledger(api_a)
    a, b, _ = seed_txs(user_a)
    api_a.post("/api/v1/transactions/assign-ledger/", {"ids": [str(a.id), str(b.id)], "ledger_id": lid}, format="json")
    s = api_a.get("/api/v1/ledgers/summaries/").data["summaries"][lid]
    assert s["count"] == 2 and round(s["net"], 2) == 887.5


def test_vendor_profile_and_1099_export(api_a, user_a):
    lid = mk_ledger(api_a, "Bob the contractor", "current_liability")
    api_a.post(
        "/api/v1/entries/",
        {
            "ledger_id": lid,
            "date": "2026-04-01",
            "description": "Pay Bob",
            "amount": "2500",
            "type": "operating_expense",
            "accounting_status": "paid",
        },
        format="json",
    )
    body = {
        "is_1099_vendor": True,
        "form_type": "nec",
        "legal_name": "Bob Smith",
        "tax_classification": "individual",
        "tin_type": "ssn",
        "tin": "123-45-6789",
        "address_line1": "1 Main",
        "city": "Austin",
        "state": "tx",
        "postal_code": "78701",
        "w9_received_at": "2026-01-02",
    }
    assert api_a.put(f"/api/v1/ledgers/{lid}/vendor-profile/", body, format="json").status_code == 200
    out = api_a.get(f"/api/v1/ledgers/{lid}/vendor-profile/?year=2026").data
    assert out["profile"]["tin_last4"] == "6789" and out["profile"]["state"] == "TX"
    s = out["summary"]
    assert s["total"] == 2500 and s["threshold"] == 2000 and s["reportable"] and s["tin_masked"] == "***-**-6789" and s["issues"] == []
    csv_ = api_a.get("/api/v1/1099/export/?year=2026").data
    assert csv_["count"] == 1 and "123-45-6789" in csv_["csv"]  # full TIN only in the filing export
    assert api_a.put(f"/api/v1/ledgers/{lid}/vendor-profile/", {**body, "tin": "12"}, format="json").status_code == 400


def test_bulk_categorize_select_all_matching(api_a, user_a, user_b):
    cid = str(cat("Sales Revenue").id)

    def mk(user, name):
        return Transaction.objects.create(
            user=user, merchant_name=name, description=name, amount=-5, transaction_date=datetime.date(2026, 2, 1), is_expense=True
        )

    mine = [mk(user_a, f"widget {i}") for i in range(3)]
    other = mk(user_b, "widget theirs")
    body = {"select_all": True, "filters": {"search": "widget"}, "category_id": cid, "expected_count": 3}
    assert api_a.post("/api/v1/transactions/bulk-categorize/", body, format="json").data["updated"] == 3
    for t in mine:
        t.refresh_from_db()
        assert str(t.user_category_id) == cid
    other.refresh_from_db()
    assert other.user_category_id is None  # another tenant is never touched
    stale = {**body, "expected_count": 99}
    assert api_a.post("/api/v1/transactions/bulk-categorize/", stale, format="json").status_code == 400
    assert api_a.post("/api/v1/transactions/bulk-categorize/", {"category_id": cid}, format="json").status_code == 400


def test_bulk_delete_removes_bank_rows_too_but_skips_ghl_and_other_tenants(api_a, user_a, user_b):
    from apps.banking.models import PlaidItem

    def mk(user, name, **kw):
        return Transaction.objects.create(
            user=user, merchant_name=name, description=name, amount=-5, transaction_date=datetime.date(2026, 2, 1), **kw
        )

    item = PlaidItem.objects.create(user=user_a, item_id="it-1", access_token_encrypted="x")
    orphan = mk(user_a, "orphan", source="plaid")
    manual = mk(user_a, "manual", source="manual")
    live = mk(user_a, "live", source="plaid", plaid_item=item)
    ghl = mk(user_a, "ghl", source="ghl")
    theirs = mk(user_b, "theirs", source="plaid")
    assert api_a.get("/api/v1/transactions/?unlinked=true").data["count"] == 1
    body = {"select_all": True, "filters": {}}
    r = api_a.post("/api/v1/transactions/bulk-delete/", body, format="json")
    assert (r.data["deleted"], r.data["skipped"], r.data["history_from"]) == (3, 1, None)  # no "to" filter: no cut-off
    assert not Transaction.objects.filter(pk__in=[orphan.pk, manual.pk, live.pk]).exists()
    assert Transaction.objects.filter(pk__in=[ghl.pk, theirs.pk]).count() == 2
    item.refresh_from_db()
    assert item.history_from is None
    assert api_a.post("/api/v1/transactions/bulk-delete/", {}, format="json").status_code == 400


def test_bulk_delete_up_to_a_date_moves_the_banks_history_start_so_a_reconnect_cannot_bring_it_back(api_a, user_a):
    from apps.banking.models import PlaidItem

    def mk(item, name, day, **kw):
        return Transaction.objects.create(
            user=user_a, merchant_name=name, description=name, amount=-5, transaction_date=day, source="plaid", plaid_item=item, **kw
        )

    bank = PlaidItem.objects.create(user=user_a, item_id="it-1", access_token_encrypted="x")
    other = PlaidItem.objects.create(user=user_a, item_id="it-2", access_token_encrypted="y")
    mk(bank, "old-1", datetime.date(2025, 3, 1))
    mk(bank, "old-2", datetime.date(2025, 12, 31))
    keep = mk(bank, "new", datetime.date(2026, 1, 2))
    older = mk(other, "ancient", datetime.date(2024, 6, 1))  # outside the filter: survives, so `other` gets no cut-off
    mk(other, "other-old", datetime.date(2025, 5, 1))
    body = {"select_all": True, "filters": {"from": "2025-01-01", "to": "2025-12-31"}, "expected_count": 3}
    r = api_a.post("/api/v1/transactions/bulk-delete/", body, format="json")
    assert (r.data["deleted"], r.data["skipped"], r.data["history_from"]) == (3, 0, datetime.date(2026, 1, 1))
    assert set(Transaction.objects.filter(user=user_a).values_list("pk", flat=True)) == {keep.pk, older.pk}
    bank.refresh_from_db()
    other.refresh_from_db()
    assert bank.history_from == datetime.date(2026, 1, 1)
    assert other.history_from is None
    # deleting an earlier range later never moves a cut-off backwards; a bank left with nothing that old gets its own
    mk(bank, "late-find", datetime.date(2024, 1, 1))
    r = api_a.post("/api/v1/transactions/bulk-delete/", {"select_all": True, "filters": {"to": "2024-12-31"}}, format="json")
    assert (r.data["deleted"], r.data["history_from"]) == (2, datetime.date(2025, 1, 1))  # late-find and `older`
    bank.refresh_from_db()
    other.refresh_from_db()
    assert bank.history_from == datetime.date(2026, 1, 1)
    assert other.history_from == datetime.date(2025, 1, 1)


def test_bulk_delete_skips_rows_in_a_completed_reconciliation(api_a, user_a):
    from apps.banking.models import BankAccount, PlaidItem, Reconciliation

    item = PlaidItem.objects.create(user=user_a, item_id="it-1", access_token_encrypted="x")
    acct = BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="chk1")
    rec = Reconciliation.objects.create(
        user=user_a, bank_account=acct, statement_date=datetime.date(2025, 6, 30), ending_balance=100, status="completed"
    )
    frozen = Transaction.objects.create(
        user=user_a,
        description="frozen",
        amount=-5,
        transaction_date=datetime.date(2025, 6, 1),
        source="plaid",
        plaid_item=item,
        reconciliation=rec,
    )
    loose = Transaction.objects.create(
        user=user_a, description="loose", amount=-5, transaction_date=datetime.date(2025, 6, 2), source="plaid", plaid_item=item
    )
    r = api_a.post("/api/v1/transactions/bulk-delete/", {"select_all": True, "filters": {"to": "2025-12-31"}}, format="json")
    # the frozen row stays, and being kept on purpose it does not stop the cut-off
    assert (r.data["deleted"], r.data["skipped"], r.data["history_from"]) == (1, 1, datetime.date(2026, 1, 1))
    assert Transaction.objects.filter(pk=frozen.pk).exists() and not Transaction.objects.filter(pk=loose.pk).exists()
    item.refresh_from_db()
    assert item.history_from == datetime.date(2026, 1, 1)


def test_equity_entries_keep_their_sign(api_a, user_a):
    """An opening balance can be a deficit and an owner draw lowers equity, so equity entries are not forced positive."""
    led = api_a.post("/api/v1/ledgers/", {"label": "Opening balance equity", "account_class": "equity"}, format="json").data["id"]
    body = {"ledger_id": led, "date": "2026-01-01", "description": "Opening", "status": "posted"}
    neg = api_a.post("/api/v1/entries/", {**body, "amount": "-5483.97", "type": "equity"}, format="json")
    pos = api_a.post("/api/v1/entries/", {**body, "amount": "100", "type": "equity"}, format="json")
    exp = api_a.post("/api/v1/entries/", {**body, "amount": "50", "type": "operating_expense"}, format="json")
    assert neg.status_code == pos.status_code == exp.status_code == 201
    assert Transaction.objects.get(pk=neg.data["id"]).amount == Decimal("-5483.97")
    assert Transaction.objects.get(pk=pos.data["id"]).amount == Decimal("100")
    assert Transaction.objects.get(pk=exp.data["id"]).amount == Decimal("-50")  # other types unchanged
