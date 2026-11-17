"""One transaction split into parts (a check paying many invoices, a deposit holding several clients' payments)."""

import datetime
from decimal import Decimal

import pytest

from apps.banking.models import BankAccount, PlaidItem
from apps.ledger.models import Category, JournalEntry, JournalLine, Ledger, Project, Transaction
from apps.ledger.services.gaap import gaap_flags

pytestmark = pytest.mark.django_db
D = datetime.date


@pytest.fixture
def bank(user_a):
    item = PlaidItem.objects.create(user=user_a, item_id="it1", access_token_encrypted="x")
    return BankAccount.objects.create(
        user=user_a, plaid_item=item, plaid_account_id="CHK", account_name="Operating", account_type="depository"
    )


def make_tx(user, amount, gaap="operating_expense", category="Office Supplies & Expenses", **kw):
    return Transaction.objects.create(
        user=user,
        amount=Decimal(amount),
        plaid_account_id=kw.pop("account", "CHK"),
        transaction_date=D(2026, 3, 1),
        source=kw.pop("source", "plaid"),
        merchant_name="Lonestar",
        user_category=Category.objects.get(user__isnull=True, name=category) if category else None,
        **gaap_flags(gaap),
        **kw,
    )


def put(api, tx, lines):
    return api.put(f"/api/v1/transactions/{tx.id}/splits/", {"lines": lines}, format="json")


def entry_lines(tx):
    e = JournalEntry.objects.get(transaction=tx)
    return list(JournalLine.objects.filter(entry=e).select_related("account", "project"))


def test_a_check_split_across_projects_posts_one_line_per_part(api_a, user_a, bank):
    p1, p2 = Project.objects.create(user=user_a, name="Alpha"), Project.objects.create(user=user_a, name="Beta")
    tx = make_tx(user_a, "-1000.00")
    r = put(
        api_a,
        tx,
        [
            {"amount": "-600.00", "reference": "INV-1", "project_id": str(p1.id)},
            {"amount": "-400.00", "reference": "INV-2", "project_id": str(p2.id)},
        ],
    )
    assert r.status_code == 200 and r.data["difference"] == "0.00" and len(r.data["lines"]) == 2
    lines = entry_lines(tx)
    assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines) == Decimal("1000.00")
    by_project = {ln.project.name: ln.debit for ln in lines if ln.project}
    assert by_project == {"Alpha": Decimal("600.00"), "Beta": Decimal("400.00")}
    bank_line = next(ln for ln in lines if ln.account.plaid_account_id == "CHK")
    assert bank_line.credit == Decimal("1000.00")  # the bank still moved the whole amount once
    assert all(ln.gaap_type == "operating_expense" for ln in lines if ln.project)


def test_each_part_can_use_its_own_category(api_a, user_a, bank):
    tx = make_tx(user_a, "-300.00")
    cat = Category.objects.get(user__isnull=True, name="Utilities")
    assert put(api_a, tx, [{"amount": "-100.00"}, {"amount": "-200.00", "category_id": str(cat.id)}]).status_code == 200
    names = sorted(ln.account.name for ln in entry_lines(tx) if ln.debit)
    assert names == ["Office Supplies & Expenses", "Utilities"]


def test_parts_must_add_up(api_a, user_a, bank):
    tx = make_tx(user_a, "-1000.00")
    r = put(api_a, tx, [{"amount": "-600.00"}, {"amount": "-300.00"}])
    assert r.status_code == 400 and "left to assign" in r.data["error"]["message"]
    assert not tx.splits.exists()
    r = put(api_a, tx, [{"amount": "-700.00"}, {"amount": "-400.00"}])
    assert r.status_code == 400 and "too much" in r.data["error"]["message"]
    assert put(api_a, tx, [{"amount": "-1000.00"}]).status_code == 400  # one part is not a split


def test_a_credit_memo_line_still_balances(api_a, user_a, bank):
    tx = make_tx(user_a, "-800.00")
    assert put(api_a, tx, [{"amount": "-1000.00"}, {"amount": "200.00", "reference": "credit memo"}]).status_code == 200
    lines = entry_lines(tx)
    assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines)
    assert sum(ln.debit - ln.credit for ln in lines if ln.account.kind == "expense") == Decimal("800.00")


def test_removing_the_split_posts_the_transaction_whole_again(api_a, user_a, bank):
    tx = make_tx(user_a, "-500.00")
    put(api_a, tx, [{"amount": "-200.00"}, {"amount": "-300.00"}])
    assert len(entry_lines(tx)) == 3
    assert put(api_a, tx, []).status_code == 200
    assert len(entry_lines(tx)) == 2


def test_a_deposit_can_be_split_between_customers(api_a, user_a, bank):
    one, two = (
        Ledger.objects.create(user=user_a, label="Acme", account_class="current_asset"),
        Ledger.objects.create(user=user_a, label="Zed", account_class="current_asset"),
    )
    tx = make_tx(user_a, "900.00", gaap="revenue", category="Sales Revenue")
    assert (
        put(api_a, tx, [{"amount": "500.00", "ledger_id": str(one.id)}, {"amount": "400.00", "ledger_id": str(two.id)}]).status_code == 200
    )
    income = [ln for ln in entry_lines(tx) if ln.account.kind == "income"]
    assert sorted(ln.credit for ln in income) == [Decimal("400.00"), Decimal("500.00")]
    assert {ln.ledger_id for ln in income} == {one.id, two.id}


def test_transfers_and_gohighlevel_rows_cannot_be_split(api_a, user_a, bank):
    t = make_tx(user_a, "-50.00", gaap="transfer", category=None)
    assert put(api_a, t, [{"amount": "-20.00"}, {"amount": "-30.00"}]).status_code == 400
    g = Transaction.objects.create(
        user=user_a, amount=Decimal("10"), transaction_date=D(2026, 3, 1), source="ghl", external_ref="ghl:inv:1:revenue"
    )
    assert put(api_a, g, [{"amount": "5"}, {"amount": "5"}]).status_code == 400


def test_you_cannot_use_someone_elses_project_or_split_their_transaction(api_a, api_b, user_a, user_b, bank):
    theirs = Project.objects.create(user=user_b, name="Theirs")
    tx = make_tx(user_a, "-100.00")
    assert put(api_a, tx, [{"amount": "-50.00", "project_id": str(theirs.id)}, {"amount": "-50.00"}]).status_code == 400
    assert put(api_b, tx, [{"amount": "-50.00"}, {"amount": "-50.00"}]).status_code == 404


def test_parts_that_stop_adding_up_are_ignored_not_posted_unbalanced(api_a, user_a, bank):
    tx = make_tx(user_a, "-1000.00")
    put(api_a, tx, [{"amount": "-600.00"}, {"amount": "-400.00"}])
    Transaction.objects.filter(pk=tx.pk).update(amount=Decimal("-900.00"))  # e.g. the bank corrected the amount
    tx.refresh_from_db()
    tx.save()
    lines = entry_lines(tx)
    assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines) == Decimal("900.00")


def test_reading_a_statement_proposes_lines_without_saving(api_a, user_a, bank):
    p = Project.objects.create(user=user_a, name="Alpha")
    tx = make_tx(user_a, "-1000.00")
    text = "Invoice,Amount,Project\nINV-1,600.00,Alpha\nINV-2,400.00,Nowhere\n"
    r = api_a.post(f"/api/v1/transactions/{tx.id}/splits/read/", {"text": text}, format="json")
    assert r.status_code == 200
    assert [ln["amount"] for ln in r.data["lines"]] == ["-600.00", "-400.00"]  # given the transaction's direction
    assert r.data["lines"][0]["project_id"] == str(p.id) and r.data["lines"][0]["reference"] == "INV-1"
    assert r.data["difference"] == "0.00"
    assert r.data["problems"] == [{"line": 3, "reason": "no project called 'Nowhere'"}]
    assert not tx.splits.exists()


def test_a_statement_without_an_amount_column_is_refused(api_a, user_a, bank):
    tx = make_tx(user_a, "-10.00")
    r = api_a.post(f"/api/v1/transactions/{tx.id}/splits/read/", {"text": "a,b\n1,2\n"}, format="json")
    assert r.status_code == 400


def test_the_transaction_list_says_how_many_parts(api_a, user_a, bank):
    tx = make_tx(user_a, "-100.00")
    put(api_a, tx, [{"amount": "-40.00"}, {"amount": "-60.00"}])
    row = next(r for r in api_a.get("/api/v1/transactions/").data["results"] if r["id"] == str(tx.id))
    assert row["split_count"] == 2


def test_a_split_check_counts_in_each_projects_own_report(api_a, user_a, bank):
    p1, p2 = Project.objects.create(user=user_a, name="Alpha"), Project.objects.create(user=user_a, name="Beta")
    tx = make_tx(user_a, "-1000.00", project=p1)  # the whole check was tagged to Alpha before it was split
    put(
        api_a,
        tx,
        [
            {"amount": "-600.00", "reference": "INV-1", "project_id": str(p1.id), "project_role": "equipment"},
            {"amount": "-400.00", "reference": "INV-2", "project_id": str(p2.id)},
        ],
    )
    a = api_a.get(f"/api/v1/projects/{p1.id}/report/").data["report"]
    b = api_a.get(f"/api/v1/projects/{p2.id}/report/").data["report"]
    assert (Decimal(str(a["total_costs"])), Decimal(str(b["total_costs"]))) == (Decimal("600.00"), Decimal("400.00"))  # not 1000 + 400
    assert a["costs"]["equipment"] == Decimal("600.00") or float(a["costs"]["equipment"]) == 600.0
    assert a["transactions"][0]["description"].endswith("INV-1")
    listed = {p["name"]: p["totals"]["total_costs"] for p in api_a.get("/api/v1/projects/").data["projects"]}
    assert float(listed["Alpha"]) == 600.0 and float(listed["Beta"]) == 400.0
