"""The balance sheet read from the journal, and the books health check shown beside it."""

import datetime
from decimal import Decimal

import pytest

from apps.banking.models import BankAccount, PlaidItem
from apps.ledger.models import Category, JournalEntry, Transaction
from apps.ledger.services.gaap import gaap_flags
from apps.ledger.services.journal import opening_balances
from apps.reporting.services.accounting import accounting_reports
from apps.reporting.services.journal_balance import books_check

pytestmark = pytest.mark.django_db
D = datetime.date
RANGE = dict(date_from=D(2026, 1, 1), date_to=D(2026, 12, 31))


def make_bank(user, plaid_id="CHK", name="Operating", kind="depository", balance="750.00"):
    item = PlaidItem.objects.filter(user=user).first() or PlaidItem.objects.create(user=user, item_id="it1", access_token_encrypted="x")
    return BankAccount.objects.create(
        user=user,
        plaid_item=item,
        plaid_account_id=plaid_id,
        account_name=name,
        account_type=kind,
        current_balance=Decimal(balance),
    )


def tx(user, amount, day, gaap, category=None, account="CHK", **extra):
    cat = Category.objects.get(user__isnull=True, name=category) if category else None
    return Transaction.objects.create(
        user=user,
        amount=Decimal(amount),
        plaid_account_id=account,
        transaction_date=day,
        source="plaid",
        merchant_name="m",
        user_category=cat,
        **gaap_flags(gaap),
        **extra,
    )


def checks(user):
    return {c["id"]: c["status"] for c in books_check(user, date_to=RANGE["date_to"])["checks"]}


def test_the_sheet_balances_and_is_healthy_when_everything_lines_up(user_a):
    make_bank(user_a)
    tx(user_a, "1000.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    tx(user_a, "-250.00", D(2026, 2, 5), "operating_expense", "Office Supplies & Expenses")
    opening_balances(user_a, apply=True)
    r = accounting_reports(user_a, **RANGE)
    bs = r["balance_sheet"]
    assert bs["unbalanced"] == 0 and bs["total_assets"] == 750.0
    assert bs["current_assets"][0] == {"id": "CHK", "label": "Operating", "balance": 750.0, "kind": "bank"}
    assert r["books_check"]["all_ok"], r["books_check"]["checks"]


def test_a_transfer_with_no_partner_balances_but_is_flagged(user_a):
    make_bank(user_a)
    make_bank(user_a, "SAV", "Savings", balance="0.00")
    tx(user_a, "-500.00", D(2026, 3, 1), "transfer", None)  # money left checking, never arrived in savings
    opening_balances(user_a, apply=True)
    r = accounting_reports(user_a, **RANGE)
    assert r["balance_sheet"]["unbalanced"] == 0  # always
    status = checks(user_a)
    assert status["transfers"] == "look" and status["banks"] == "ok"
    assert "$500.00" in next(c["detail"] for c in r["books_check"]["checks"] if c["id"] == "transfers")


def test_a_bank_that_disagrees_with_the_books_is_flagged(user_a):
    bank = make_bank(user_a)
    tx(user_a, "1000.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    opening_balances(user_a, apply=True)
    assert checks(user_a)["banks"] == "ok"
    BankAccount.objects.filter(pk=bank.pk).update(current_balance=Decimal("900.00"))  # the bank moved on without us
    assert checks(user_a)["banks"] == "look"
    assert books_check(user_a, date_to=RANGE["date_to"])["banks"][0]["difference"] == -150.0


def test_a_transaction_with_no_entry_is_counted(user_a):
    make_bank(user_a)
    t = tx(user_a, "100.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    opening_balances(user_a, apply=True)
    assert checks(user_a)["missing"] == "ok"
    JournalEntry.objects.filter(transaction=t).delete()  # as if posting had failed
    assert checks(user_a)["missing"] == "look"


def test_money_waiting_in_gohighlevel_is_flagged(user_a):
    Transaction.objects.create(
        user=user_a,
        amount=Decimal("300.00"),
        transaction_date=D(2026, 4, 1),
        source="ghl",
        external_ref="ghl:pay:p1",
        merchant_name="Invoice paid",
        **gaap_flags("asset"),
    )
    assert checks(user_a)["ghl"] == "look"
    labels = [r["label"] for r in accounting_reports(user_a, **RANGE)["balance_sheet"]["current_assets"]]
    assert "Collected through GoHighLevel, not yet in a bank account" in labels


def test_a_starting_balance_bigger_than_all_activity_is_flagged(user_a):
    make_bank(user_a, balance="50000.00")
    tx(user_a, "100.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    opening_balances(user_a, apply=True)
    assert checks(user_a)["opening"] == "look"


def test_a_card_shows_as_a_liability_in_owed_terms(user_a):
    make_bank(user_a, "CC", "Visa", kind="credit", balance="200.00")
    tx(user_a, "-200.00", D(2026, 2, 1), "operating_expense", "Office Supplies & Expenses", account="CC")
    opening_balances(user_a, apply=True)
    bs = accounting_reports(user_a, **RANGE)["balance_sheet"]
    assert bs["current_liabilities"] == [{"id": "CC", "label": "Visa", "balance": 200.0, "kind": "bank"}]
    assert bs["unbalanced"] == 0


def test_users_without_entries_keep_the_single_entry_sheet(user_a):
    make_bank(user_a)
    Transaction.objects.bulk_create(  # bulk_create skips saving signals: no journal entries yet, as before a backfill
        [Transaction(user=user_a, amount=Decimal("10.00"), plaid_account_id="CHK", transaction_date=D(2026, 2, 1), source="plaid")]
    )
    assert not JournalEntry.objects.filter(user=user_a).exists()
    r = accounting_reports(user_a, **RANGE)
    assert r["books_check"] is None
    assert r["balance_sheet"]["current_assets"][0]["balance"] == 750.0  # the bank's own balance, as the old sheet reads it


def test_the_switch_can_be_turned_off(user_a, settings):
    make_bank(user_a)
    tx(user_a, "1000.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    settings.BALANCE_SHEET_FROM_JOURNAL = False
    assert accounting_reports(user_a, **RANGE)["books_check"] is None


# ---- the other statements, read from the journal


def test_profit_and_loss_by_project_follows_the_parts_of_a_split(api_a, user_a):
    from apps.ledger.models import Project

    make_bank(user_a)
    a, b = Project.objects.create(user=user_a, name="Alpha"), Project.objects.create(user=user_a, name="Beta")
    t = tx(user_a, "-1000.00", D(2026, 3, 1), "operating_expense", "Office Supplies & Expenses")
    r = api_a.put(
        f"/api/v1/transactions/{t.id}/splits/",
        {"lines": [{"amount": "-600.00", "project_id": str(a.id)}, {"amount": "-400.00", "project_id": str(b.id)}]},
        format="json",
    )
    assert r.status_code == 200
    base = "/api/v1/reports/accounting/?from=2026-01-01&to=2026-12-31"
    assert api_a.get(base).data["profit_and_loss"]["total_expenses"] == 1000
    assert api_a.get(f"{base}&project_id={a.id}").data["profit_and_loss"]["total_expenses"] == 600
    assert api_a.get(f"{base}&project_id={b.id}").data["profit_and_loss"]["total_expenses"] == 400


def test_other_income_and_cogs_stay_in_their_own_sections(user_a):
    make_bank(user_a)
    tx(user_a, "500.00", D(2026, 2, 1), "other_income", "Interest Income")
    tx(user_a, "-120.00", D(2026, 2, 2), "cogs", "Cost of Goods Sold")
    pl = accounting_reports(user_a, **RANGE)["profit_and_loss"]
    assert pl["total_other_income"] == 500 and pl["total_cogs"] == 120 and pl["total_revenue"] == 0
    assert [ln["name"] for ln in pl["cogs"]] == ["Cost of Goods Sold"]


def test_cash_flow_trial_balance_and_general_ledger_come_from_the_journal(user_a):
    make_bank(user_a)
    make_bank(user_a, "SAV", "Savings", balance="0.00")
    tx(user_a, "1000.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    tx(user_a, "-250.00", D(2026, 2, 5), "operating_expense", "Office Supplies & Expenses")
    tx(user_a, "-100.00", D(2026, 2, 6), "transfer", None)  # to savings
    tx(user_a, "100.00", D(2026, 2, 6), "transfer", None, account="SAV")
    opening_balances(user_a, apply=True)
    r = accounting_reports(user_a, **RANGE)
    cf = r["cash_flow"]
    assert cf["operating"] == 750 and cf["net_change"] == 750  # the transfer is cash moving between your own accounts: no flow
    assert {ln["name"]: ln["value"] for ln in cf["operating_lines"]} == {"Sales Revenue": 1000, "Office Supplies & Expenses": -250}
    assert r["trial_balance"]["balanced"]
    gl = {b["label"]: b for b in r["general_ledger"]}
    assert 1000 in [e["debit"] for e in gl["Operating"]["entries"]]
    assert gl["Operating"]["closing"] == 750  # the bank's balance: the starting balance is a line of its own


# ---- read-only views of the books


def test_a_transaction_shows_the_entry_it_was_posted_as(api_a, user_a):
    make_bank(user_a)
    t = tx(user_a, "-50.00", D(2026, 2, 1), "operating_expense", "Office Supplies & Expenses")
    r = api_a.get(f"/api/v1/transactions/{t.id}/entry/").data
    assert r["posted"] and r["total_debit"] == r["total_credit"] == Decimal("50.00")
    assert {(ln["account"], ln["debit"], ln["credit"]) for ln in r["lines"]} == {
        ("Office Supplies & Expenses", Decimal("50.00"), Decimal("0.00")),
        ("Operating", Decimal("0.00"), Decimal("50.00")),
    }


def test_a_transaction_not_in_the_books_says_why(api_a, user_a):
    make_bank(user_a)
    t = tx(user_a, "-9.00", D(2026, 2, 1), "operating_expense", "Office Supplies & Expenses", pending=True)
    r = api_a.get(f"/api/v1/transactions/{t.id}/entry/").data
    assert r["posted"] is False and "pending" in r["reason"]


def test_chart_of_accounts_lists_accounts_with_balances_and_is_private(api_a, api_b, user_a):
    make_bank(user_a)
    tx(user_a, "1000.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    tx(user_a, "-250.00", D(2026, 2, 5), "operating_expense", "Office Supplies & Expenses")
    rows = {a["name"]: a for a in api_a.get("/api/v1/chart-of-accounts/").data["accounts"]}
    assert rows["Operating"]["balance"] == 750 and rows["Operating"]["bank_account"]
    assert rows["Sales Revenue"]["balance"] == 1000 and rows["Office Supplies & Expenses"]["balance"] == 250
    assert api_b.get("/api/v1/chart-of-accounts/").data["accounts"] == []
    t = Transaction.objects.filter(user=user_a).first()
    assert api_b.get(f"/api/v1/transactions/{t.id}/entry/").status_code == 404
