"""Tools around the books: a payee typed in by the user, opening balances set by hand, new categories on the chart of accounts,
transfers with a missing side, and the filters behind the balance-sheet links."""

import datetime
from decimal import Decimal

import pytest

from apps.ledger.models import Account, JournalEntry, Transaction
from apps.ledger.services.journal import opening_balances
from apps.ledger.services.transactions import apply_filters
from apps.reporting.services.accounting import accounting_reports
from tests.test_journal_balance import RANGE, make_bank, tx

pytestmark = pytest.mark.django_db
D = datetime.date


def books_balance(user, name):
    a = Account.objects.get(user=user, name=name)
    return sum(ln.debit - ln.credit for ln in a.lines.all())


def test_payee_is_saved_searchable_and_exported(api_a, user_a):
    make_bank(user_a)
    t = tx(user_a, "-2244.00", D(2026, 3, 1), "operating_expense")
    Transaction.objects.filter(pk=t.pk).update(merchant_name="CHECK #1041")
    assert api_a.patch(f"/api/v1/transactions/{t.id}/", {"payee": "  Pilot Communications "}, format="json").status_code == 200
    t.refresh_from_db()
    assert t.payee == "Pilot Communications" and t.merchant_name == "CHECK #1041"
    assert JournalEntry.objects.get(transaction=t).memo == "Pilot Communications"
    rows = api_a.get("/api/v1/transactions/?search=pilot").json()
    assert [r["payee"] for r in rows.get("results", rows.get("rows", []))] == ["Pilot Communications"]
    assert "Pilot Communications" in api_a.get("/api/v1/transactions/export/?output=csv").content.decode()
    api_a.patch(f"/api/v1/transactions/{t.id}/", {"payee": ""}, format="json")
    t.refresh_from_db()
    assert t.payee is None


def test_opening_balance_set_by_hand_is_kept_until_reset(api_a, api_b, user_a):
    make_bank(user_a, balance="750.00")
    tx(user_a, "300.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    opening_balances(user_a, apply=True)  # worked out: 450
    assert books_balance(user_a, "Operating") == Decimal("750.00")

    r = api_a.post("/api/v1/opening-balances/", {"plaid_account_id": "CHK", "amount": "1000.00", "date": "2025-12-31"}, format="json")
    assert r.status_code == 200, r.content
    row = r.json()["accounts"][0]
    assert row["manual"] is True and Decimal(str(row["current"])) == Decimal("1000.00") and row["current_date"] == "2025-12-31"
    assert books_balance(user_a, "Operating") == Decimal("1300.00")
    opening_balances(user_a, apply=True)  # the automatic step must not undo it
    assert books_balance(user_a, "Operating") == Decimal("1300.00")
    assert JournalEntry.objects.filter(user=user_a, kind__in=("opening", "opening_set")).count() == 1

    r = api_a.post("/api/v1/opening-balances/", {"plaid_account_id": "CHK", "reset": True}, format="json")
    assert r.json()["accounts"][0]["manual"] is False
    assert books_balance(user_a, "Operating") == Decimal("750.00")

    assert api_b.post("/api/v1/opening-balances/", {"plaid_account_id": "CHK", "amount": "5.00"}, format="json").status_code == 400
    assert api_a.post("/api/v1/opening-balances/", {"plaid_account_id": "CHK"}, format="json").status_code == 400


def test_a_card_opening_balance_is_the_amount_owed(user_a):
    from apps.ledger.services.journal import set_opening_balance

    make_bank(user_a, plaid_id="CARD", name="Visa", kind="credit", balance="200.00")
    set_opening_balance(user_a, "CARD", Decimal("150.00"))
    assert books_balance(user_a, "Visa") == Decimal("-150.00")  # a liability: credits exceed debits


def test_a_new_category_is_on_the_chart_of_accounts_at_once_and_follows_a_rename(api_a, user_a):
    r = api_a.post("/api/v1/categories/", {"name": "Drone Parts", "type": "operating_expense"}, format="json")
    assert r.status_code == 201, r.content
    acc = Account.objects.get(user=user_a, category_id=r.json()["id"])
    assert (acc.name, acc.kind) == ("Drone Parts", "expense")
    assert "Drone Parts" in [a["name"] for a in api_a.get("/api/v1/chart-of-accounts/").json()["accounts"]]
    r2 = api_a.patch(f"/api/v1/categories/{r.json()['id']}/", {"name": "Drone Spares"}, format="json")
    assert r2.status_code == 200, r2.content
    acc.refresh_from_db()
    assert acc.name == "Drone Spares"


def test_unmatched_transfers_are_listed_per_account_with_the_likely_other_side(api_a, user_a):
    make_bank(user_a)
    make_bank(user_a, plaid_id="SAV", name="Savings")
    tx(user_a, "-500.00", D(2026, 3, 1), "transfer")  # matched pair
    tx(user_a, "500.00", D(2026, 3, 1), "transfer", account="SAV")
    out = tx(user_a, "-800.00", D(2026, 3, 5), "transfer")  # other side was filed as income
    other = tx(user_a, "800.00", D(2026, 3, 6), "revenue", "Sales Revenue", account="SAV")
    lonely = tx(user_a, "-90.00", D(2026, 3, 9), "transfer")  # other side is nowhere
    tx(user_a, "90.00", D(2026, 3, 9), "revenue", "Sales Revenue")  # same account: not a candidate
    groups = api_a.get("/api/v1/transactions/unmatched-transfers/").json()["accounts"]
    assert len(groups) == 1 and groups[0]["account_id"] == "CHK" and Decimal(str(groups[0]["total"])) == Decimal("-890.00")
    by_id = {r["id"]: r for r in groups[0]["rows"]}
    assert by_id[str(out.id)]["suggestion"]["id"] == str(other.id)
    assert by_id[str(out.id)]["suggestion"]["account"] == "Savings"
    assert by_id[str(lonely.id)]["suggestion"] is None
    assert api_a.post("/api/v1/transactions/link-transfer/", {"ids": [str(out.id), str(other.id)]}, format="json").status_code == 200
    rows = api_a.get("/api/v1/transactions/unmatched-transfers/").json()["accounts"][0]["rows"]
    assert [r["id"] for r in rows] == [str(lonely.id)]


def test_balance_sheet_links_for_manual_cash_and_uncategorized_and_only_open_receivables(user_a):
    make_bank(user_a)
    cash = Transaction.objects.create(
        user=user_a, amount=Decimal("120.00"), transaction_date=D(2026, 4, 1), source="manual", gaap_type="revenue", is_income=True
    )
    uncategorized = tx(user_a, "-40.00", D(2026, 4, 2), "equity")
    kinds = {
        (ln["kind"], ln["id"])
        for group in ("current_assets", "equity")
        for ln in accounting_reports(user_a, **RANGE)["balance_sheet"][group]
        if "kind" in ln
    }
    assert ("books_account", "manual_cash") in kinds and ("books_account", "uncategorized_equity") in kinds
    mine = Transaction.objects.filter(user=user_a)
    assert list(apply_filters(mine, {"books_account": "manual_cash"})) == [cash]
    assert list(apply_filters(mine, {"books_account": "uncategorized_equity"})) == [uncategorized]
    assert apply_filters(mine, {"books_account": "transfer_clearing"}).count() == mine.count()  # not an allowed key: ignored

    def ghl(ref, amount, description):
        return Transaction.objects.create(
            user=user_a,
            amount=Decimal(amount),
            transaction_date=D(2026, 5, 1),
            source="ghl",
            external_ref=ref,
            description=description,
            gaap_type="revenue" if ref.endswith("revenue") else "asset",
        )

    ghl("ghl:inv:a:revenue", "100.00", "Invoice 0007 — revenue")  # paid in full: not open
    ghl("ghl:pay:p1", "100.00", "Payment received — invoice 0007")
    open_invoice = ghl("ghl:inv:b:revenue", "300.00", "Invoice 0008 — revenue")
    part_payment = ghl("ghl:pay:p2", "50.00", "Payment received — invoice 0008")
    found = set(apply_filters(Transaction.objects.filter(user=user_a), {"books_unassigned": "receivable"}))
    assert found == {open_invoice, part_payment}
