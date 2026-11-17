"""Reports built from journal entries, and the side-by-side check against the current reports."""

import datetime
from decimal import Decimal

import pytest

from apps.banking.models import BankAccount, PlaidItem
from apps.ledger.models import Category, Transaction
from apps.ledger.services.gaap import gaap_flags
from apps.ledger.services.journal import opening_balances
from apps.reporting.services.ledger_reports import compare_with_current, entries_reports

pytestmark = pytest.mark.django_db
D = datetime.date
RANGE = dict(date_from=D(2026, 1, 1), date_to=D(2026, 12, 31))


@pytest.fixture
def bank(user_a):
    item = PlaidItem.objects.create(user=user_a, item_id="it1", access_token_encrypted="x")
    return BankAccount.objects.create(
        user=user_a,
        plaid_item=item,
        plaid_account_id="CHK",
        account_name="Operating",
        account_type="depository",
        current_balance=Decimal("750.00"),
    )


def tx(user, amount, day, gaap, category):
    return Transaction.objects.create(
        user=user,
        amount=Decimal(amount),
        plaid_account_id="CHK",
        transaction_date=day,
        source="plaid",
        merchant_name="m",
        user_category=Category.objects.get(user__isnull=True, name=category),
        **gaap_flags(gaap),
    )


def test_journal_reports_balance_and_match_the_current_reports(user_a, bank):
    tx(user_a, "1000.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    tx(user_a, "-250.00", D(2026, 2, 5), "operating_expense", "Office Supplies & Expenses")
    opening_balances(user_a, apply=True)  # bank says 750, transactions add up to 750: no opening needed
    r = entries_reports(user_a, **RANGE)
    assert r["profit_and_loss"]["net_profit"] == 750.0
    assert r["balance_sheet"]["total_assets"] == 750.0 and r["balance_sheet"]["unbalanced"] == 0
    assert r["trial_balance"]["balanced"]
    result = compare_with_current(user_a, **RANGE)
    assert result["matches"], result["rows"]


def test_a_mismatch_is_reported_not_hidden(user_a, bank):
    tx(user_a, "1000.00", D(2026, 2, 1), "revenue", "Sales Revenue")
    result = compare_with_current(user_a, **RANGE)  # no opening balance: books say 1000, bank says 750
    assert not result["matches"]
    assert {r["measure"] for r in result["rows"] if r["difference"]} >= {"Total assets"}


def test_a_ghl_payment_is_not_counted_as_income_on_top_of_its_invoice(user_a):
    from apps.reporting.services.accounting import accounting_reports

    other = Category.objects.get(user__isnull=True, name="Other Income")
    for ref, amount in (("ghl:inv:1:revenue", "100.00"), ("ghl:pay:p1", "100.00")):
        Transaction.objects.create(
            user=user_a,
            amount=Decimal(amount),
            transaction_date=D(2026, 3, 1),
            source="ghl",
            external_ref=ref,
            user_category=other,
            **gaap_flags("other_income"),
        )
    assert accounting_reports(user_a, **RANGE)["profit_and_loss"]["total_income"] == 100.0
    assert compare_with_current(user_a, **RANGE)["rows"][0]["difference"] == 0


def test_transfers_and_reconciliation_read_the_same_from_the_journal(user_a, bank):
    from apps.banking.models import Reconciliation
    from apps.reporting.services.ledger_reports import reconciliation_check

    item = bank.plaid_item
    card = BankAccount.objects.create(
        user=user_a, plaid_item=item, plaid_account_id="CARD", account_name="Visa", account_type="credit", current_balance=Decimal("0")
    )
    # 200 moves from checking to the card; only the checking side has arrived so far
    out = Transaction.objects.create(
        user=user_a,
        amount=Decimal("-200.00"),
        plaid_account_id="CHK",
        transaction_date=D(2026, 4, 1),
        source="plaid",
        merchant_name="to card",
        **gaap_flags("transfer"),
    )
    charge = tx(user_a, "-30.00", D(2026, 4, 2), "operating_expense", "Office Supplies & Expenses")
    card_charge = Transaction.objects.create(
        user=user_a,
        amount=Decimal("-45.00"),
        plaid_account_id="CARD",
        transaction_date=D(2026, 4, 3),
        source="plaid",
        merchant_name="shop",
        **gaap_flags("operating_expense"),
    )
    rec = Reconciliation.objects.create(bank_account=bank, user=user_a, statement_date=D(2026, 4, 30), ending_balance=Decimal("-230.00"))
    Transaction.objects.filter(pk__in=[out.pk, charge.pk]).update(reconciliation=rec)
    card_rec = Reconciliation.objects.create(bank_account=card, user=user_a, statement_date=D(2026, 4, 30), ending_balance=Decimal("45.00"))
    Transaction.objects.filter(pk=card_charge.pk).update(reconciliation=card_rec)

    result = compare_with_current(user_a, **RANGE)
    by = {r["measure"]: r for r in result["rows"]}
    assert by["Transfers: money out"]["entries"] == 200.0 and by["Transfers: money out"]["difference"] == 0
    assert by["Transfers: difference"]["entries"] == -200.0  # the card side is missing
    check = reconciliation_check(user_a)
    assert len(check["rows"]) == 2 and check["matches"], check


def test_balance_sheet_shows_money_collected_through_ghl_but_not_in_a_bank(user_a):
    from apps.ledger.services.journal import GHL_CLEARING_LABEL
    from apps.reporting.services.accounting import accounting_reports

    for ref, amount in (("ghl:inv:1:revenue", "100.00"), ("ghl:pay:p1", "100.00"), ("ghl:fee:p1", "-3.00")):
        Transaction.objects.create(
            user=user_a,
            amount=Decimal(amount),
            transaction_date=D(2026, 3, 1),
            source="ghl",
            external_ref=ref,
            **gaap_flags("revenue" if "inv" in ref else "operating_expense"),
        )
    line = next(r for r in accounting_reports(user_a, **RANGE)["balance_sheet"]["current_assets"] if r["id"] == "ghl_clearing")
    assert line["label"] == GHL_CLEARING_LABEL and line["balance"] == 97.0
    assert compare_with_current(user_a, **RANGE)["rows"][3]["difference"] == 0  # total assets agree with the journal
