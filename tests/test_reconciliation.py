import datetime
from decimal import Decimal

import pytest

from apps.banking.models import BankAccount, PlaidItem
from apps.ledger.models import Transaction

pytestmark = pytest.mark.django_db
D = datetime.date


def account(user, kind="depository", pid="acct-1"):
    item = PlaidItem.objects.create(user=user, item_id=f"item-{pid}", access_token_encrypted="x")
    return BankAccount.objects.create(user=user, plaid_item=item, plaid_account_id=pid, account_name="Checking", account_type=kind)


def tx(user, acct, amount, day, **kw):
    return Transaction.objects.create(
        user=user,
        plaid_item=acct.plaid_item,
        plaid_account_id=acct.plaid_account_id,
        merchant_name=f"t{amount}",
        amount=amount,
        transaction_date=D(2026, 3, day),
        **kw,
    )


def start(api, acct, ending, date="2026-03-31", beginning=1000):
    r = api.post(
        "/api/v1/reconciliations/",
        {"account_id": str(acct.id), "statement_date": date, "ending_balance": ending, "beginning_balance": beginning},
        format="json",
    )
    assert r.status_code == 201, r.data
    return r.data["reconciliation"]["id"]


def clear(api, rid, txs, cleared=True):
    return api.post(f"/api/v1/reconciliations/{rid}/clear/", {"ids": [str(t.id) for t in txs], "cleared": cleared}, format="json")


def test_checking_account_reconciles_and_locks(api_a, user_a):
    acct = account(user_a)
    paycheck, rent, later = tx(user_a, acct, 500, 5), tx(user_a, acct, -200, 9), tx(user_a, acct, -50, 28)
    rid = start(api_a, acct, 1300)  # 1000 + 500 - 200
    data = clear(api_a, rid, [paycheck]).data["reconciliation"]
    assert Decimal(data["difference"]) == Decimal("-200") and not data["balanced"]
    assert api_a.post(f"/api/v1/reconciliations/{rid}/finish/").status_code == 400  # not balanced yet
    data = clear(api_a, rid, [rent]).data["reconciliation"]
    assert data["balanced"] and Decimal(data["cleared_balance"]) == 1300
    assert api_a.post(f"/api/v1/reconciliations/{rid}/finish/").data["reconciliation"]["status"] == "completed"
    # locked: cannot hide a reconciled row, but categorising is still fine; unreconciled rows stay editable
    assert api_a.patch(f"/api/v1/transactions/{rent.id}/", {"is_hidden": True}, format="json").status_code == 400
    assert api_a.patch(f"/api/v1/transactions/{later.id}/", {"is_hidden": True}, format="json").status_code == 200
    # next statement starts from the verified ending balance and cannot be back-dated
    r = api_a.post(
        "/api/v1/reconciliations/", {"account_id": str(acct.id), "statement_date": "2026-03-15", "ending_balance": 0}, format="json"
    )
    assert r.status_code == 400
    rid2 = start(api_a, acct, 1250, date="2026-04-30", beginning=999)
    assert Decimal(api_a.get(f"/api/v1/reconciliations/{rid2}/").data["reconciliation"]["beginning_balance"]) == 1300


def test_credit_card_uses_statement_terms(api_a, user_a):
    card = account(user_a, "credit", "card-1")
    charge, payment = tx(user_a, card, -120, 3), tx(user_a, card, 100, 20)  # stored in account view
    rid = start(api_a, card, 70, beginning=50)  # owed: 50 + 120 charge - 100 payment = 70
    data = clear(api_a, rid, [charge, payment]).data["reconciliation"]
    assert Decimal(data["cleared_balance"]) == 70 and data["balanced"]
    assert (Decimal(data["additions"]), Decimal(data["subtractions"])) == (120, 100)


def test_reopen_delete_and_isolation(api_a, api_b, user_a, user_b):
    acct = account(user_a)
    t = tx(user_a, acct, 10, 2)
    rid = start(api_a, acct, 1010)
    clear(api_a, rid, [t])
    assert api_b.get(f"/api/v1/reconciliations/{rid}/").status_code == 404
    assert api_b.post(f"/api/v1/reconciliations/{rid}/finish/").status_code == 404
    api_a.post(f"/api/v1/reconciliations/{rid}/finish/")
    assert api_a.delete(f"/api/v1/reconciliations/{rid}/").status_code == 400  # must reopen first
    assert api_a.post(f"/api/v1/reconciliations/{rid}/reopen/").data["reconciliation"]["status"] == "in_progress"
    assert api_a.delete(f"/api/v1/reconciliations/{rid}/").status_code == 204
    t.refresh_from_db()
    assert t.reconciliation_id is None  # released
    # another tenant's transactions can never be ticked
    foreign = account(user_b, pid="acct-b")
    ft = tx(user_b, foreign, 5, 2)
    rid2 = start(api_a, acct, 1000)
    clear(api_a, rid2, [ft])
    ft.refresh_from_db()
    assert ft.reconciliation_id is None
