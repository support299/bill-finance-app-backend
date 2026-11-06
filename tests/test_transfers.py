"""Pairing the two sides of a move between the user's own accounts (e.g. paying the card from checking)."""

import datetime

import pytest

from apps.ledger.models import Category, Transaction
from apps.ledger.services.gaap import gaap_flags
from apps.ledger.services.transfers import pair_transfers

pytestmark = pytest.mark.django_db

D = datetime.date


def row(user, amount, account, day, **kw):
    base = dict(
        user=user,
        amount=amount,
        plaid_account_id=account,
        transaction_date=day,
        source="plaid",
        merchant_name=f"{account} {amount}",
        category_source="plaid",
        **gaap_flags("operating_expense" if amount < 0 else "other_income"),
    )
    return Transaction.objects.create(**{**base, **kw})


def is_transfer(t):
    t.refresh_from_db()
    return (
        t.is_transfer
        and t.gaap_type == "transfer"
        and t.user_category.name == "Internal Transfer"
        and t.category_source == "transfer_match"
    )


def test_card_payment_from_checking_is_paired_and_kept_out_of_the_pl(api_a, user_a):
    out = row(user_a, -500, "chk", D(2026, 3, 1))
    inn = row(user_a, 500, "card", D(2026, 3, 3))  # lands two days later
    groceries = row(user_a, -500, "card", D(2026, 3, 2))  # same amount, but an outflow: not the other side
    assert pair_transfers(user_a) == 2
    assert is_transfer(out) and is_transfer(inn) and not is_transfer(groceries)
    pl = api_a.get("/api/v1/reports/accounting/?from=2026-03-01&to=2026-03-31").data["profit_and_loss"]
    assert pl["total_expenses"] == 500  # only the groceries; the payment is no longer an expense
    assert pair_transfers(user_a) == 0  # idempotent


def test_pairing_respects_accounts_window_and_user_choices(user_a, user_b):
    same = row(user_a, -40, "chk", D(2026, 3, 1))
    same_in = row(user_a, 40, "chk", D(2026, 3, 1))  # same account: a refund, not a transfer
    late = row(user_a, -70, "chk", D(2026, 3, 1))
    late_in = row(user_a, 70, "card", D(2026, 3, 9))  # outside the window
    chosen = row(user_a, -90, "chk", D(2026, 3, 1), category_source="user_rule")  # the user decided
    chosen_in = row(user_a, 90, "card", D(2026, 3, 1))
    theirs = row(user_b, 90, "card", D(2026, 3, 1))  # another tenant never matches
    job = row(user_a, -120, "chk", D(2026, 3, 1), project_role="other")
    assert pair_transfers(user_a) == 0
    for t in (same, same_in, late, late_in, chosen, chosen_in, theirs, job):
        assert not is_transfer(t)


def test_one_inflow_pairs_with_only_one_outflow(user_a):
    a = row(user_a, -100, "chk", D(2026, 3, 1))
    b = row(user_a, -100, "chk", D(2026, 3, 2))
    inn = row(user_a, 100, "card", D(2026, 3, 2))
    assert pair_transfers(user_a) == 2
    assert is_transfer(a) and is_transfer(inn) and not is_transfer(b)  # earliest outflow wins


def test_frozen_rows_are_left_alone(user_a):
    from apps.banking.models import BankAccount, PlaidItem, Reconciliation

    item = PlaidItem.objects.create(user=user_a, item_id="it", access_token_encrypted="x")
    acct = BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="chk")
    rec = Reconciliation.objects.create(user=user_a, bank_account=acct, statement_date=D(2026, 3, 31), ending_balance=1, status="completed")
    out = row(user_a, -500, "chk", D(2026, 3, 1), reconciliation=rec)
    inn = row(user_a, 500, "card", D(2026, 3, 1))
    assert pair_transfers(user_a) == 0
    assert not is_transfer(out) and not is_transfer(inn)


def test_detect_endpoint_and_recategorize_pair_transfers(api_a, user_a):
    out = row(user_a, -300, "chk", D(2026, 3, 1))
    inn = row(user_a, 300, "card", D(2026, 3, 1))
    assert api_a.post("/api/v1/transactions/detect-transfers/", {}, format="json").data == {"updated": 2}
    assert is_transfer(out) and is_transfer(inn)
    # recategorize re-runs the categorizer (which would call these expenses again) and then re-pairs them
    assert api_a.post("/api/v1/transactions/recategorize/", {}, format="json").status_code == 200
    assert is_transfer(out) and is_transfer(inn)
    assert Category.objects.get(user__isnull=True, name="Internal Transfer").type == "transfer"
