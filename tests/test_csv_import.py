import pytest

from apps.banking.models import BankAccount, PlaidItem
from apps.banking.services.csv_import import CsvImportError, parse_csv
from apps.ledger.models import Transaction

pytestmark = pytest.mark.django_db

URL = "/api/v1/accounts/csv-import/"
CSV = """Date,Description,Amount
03/01/2026,Starbucks,-12.50
03/02/2026,Client payment,"1,900.00"
03/02/2026,Coffee,-3.00
03/02/2026,Coffee,-3.00
"""


def upload(api, text=CSV, **kw):
    body = {"csv_text": text, "institution_name": "Small CU", "account_name": "Checking", **kw}
    return api.post(URL, body, format="json")


def test_import_creates_account_and_transactions(api_a, user_a):
    r = upload(api_a)
    assert r.status_code == 201, r.data
    assert (r.data["added"], r.data["duplicates"]) == (4, 0)
    acc = BankAccount.objects.get(user=user_a)
    assert acc.plaid_item.connection_status == "csv" and acc.plaid_item.access_token_encrypted == ""
    rows = Transaction.objects.filter(user=user_a, plaid_account_id=acc.plaid_account_id)
    assert rows.count() == 4 and set(rows.values_list("source", flat=True)) == {"csv"}
    assert rows.get(description="Starbucks").amount == -12.5 and rows.get(description="Client payment").amount == 1900
    assert rows.get(description="Client payment").is_income and rows.get(description="Starbucks").is_expense


def test_reupload_creates_no_duplicates(api_a, user_a):
    first = upload(api_a)
    again = upload(api_a, account_id=first.data["account_id"])
    assert again.status_code == 200 and (again.data["added"], again.data["duplicates"]) == (0, 4)
    assert Transaction.objects.filter(user=user_a).count() == 4  # both identical coffees kept once each


def test_overlapping_upload_adds_only_new_rows(api_a, user_a):
    acc = upload(api_a).data["account_id"]
    r = upload(api_a, "Date,Description,Amount\n03/02/2026,Coffee,-3.00\n03/05/2026,Rent,-1000\n", account_id=acc)
    assert (r.data["added"], r.data["duplicates"]) == (1, 1)


def test_user_edits_survive_reupload(api_a, user_a):
    acc = upload(api_a).data["account_id"]
    Transaction.objects.filter(user=user_a, description="Starbucks").update(is_hidden=True, description="Edited")
    upload(api_a, account_id=acc)
    assert Transaction.objects.filter(user=user_a, description="Edited", is_hidden=True).count() == 1
    assert Transaction.objects.filter(user=user_a).count() == 4


def test_dry_run_saves_nothing(api_a, user_a):
    r = upload(api_a, dry_run=True)
    assert r.status_code == 200 and r.data["added"] == 4
    assert not Transaction.objects.filter(user=user_a).exists() and not BankAccount.objects.filter(user=user_a).exists()


def test_cannot_import_into_plaid_account_or_other_users_account(api_a, api_b, user_a):
    item = PlaidItem.objects.create(user=user_a, item_id="i", access_token_encrypted="x", connection_status="good")
    plaid_acc = BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="p1", account_name="P")
    assert upload(api_a, account_id=str(plaid_acc.id)).status_code == 400
    mine = upload(api_a).data["account_id"]
    assert upload(api_b, account_id=mine).status_code == 400


def test_debit_credit_columns_and_inverted_sign():
    p = parse_csv(b"Account: 1234\n\nPosting Date,Details,Debit,Credit\n2026-03-01,Fuel,40.00,\n2026-03-02,Refund,,15.25\n")
    assert [(str(r.amount), r.description) for r in p.rows] == [("-40.00", "Fuel"), ("15.25", "Refund")]
    card = parse_csv(b"Date,Description,Amount\n2026-03-01,Fuel,40.00\n", invert_sign=True)
    assert str(card.rows[0].amount) == "-40.00"


def test_bad_rows_reported_and_bad_files_rejected():
    p = parse_csv(b"Date,Description,Amount\n2026-03-01,Ok,1\n,No date,2\n2026-03-03,No amount,\n")
    assert len(p.rows) == 1 and [s["line"] for s in p.skipped] == [3, 4]
    for bad in (b"", b"foo,bar\n1,2\n", b"Date,Description,Amount\n"):
        with pytest.raises(CsvImportError):
            parse_csv(bad)
