"""Replaces the Supabase RLS guarantees: user B must never see or change user A's data."""

import datetime

import pytest

from apps.banking.models import BankAccount, PlaidItem
from apps.core.crypto import encrypt_secret
from apps.core.models import Notification
from apps.ledger.models import Category, CategoryRule, Ledger, LedgerTag, Transaction, VendorTaxProfile

pytestmark = pytest.mark.django_db


@pytest.fixture
def data_a(user_a):
    ledger = Ledger.objects.create(user=user_a, label="Acme", account_class="current_asset")
    tag = LedgerTag.objects.create(user=user_a, name="vip")
    ledger.tags.add(tag)
    cat = Category.objects.create(user=user_a, name="My cat", type="operating_expense")
    rule = CategoryRule.objects.create(user=user_a, keyword="acme", category=cat)
    tx = Transaction.objects.create(user=user_a, amount=-10, transaction_date=datetime.date(2026, 1, 5), ledger=ledger, source="manual")
    item = PlaidItem.objects.create(user=user_a, item_id="item-a", access_token_encrypted=encrypt_secret("tok"))
    BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="acc-a")
    note = Notification.objects.create(user=user_a, title="hi")
    VendorTaxProfile.objects.create(user=user_a, ledger=ledger, is_1099_vendor=True)
    return dict(ledger=ledger, tag=tag, cat=cat, rule=rule, tx=tx, item=item, note=note)


def test_lists_are_empty_for_other_users(api_b, data_a):
    assert api_b.get("/api/v1/ledgers/").data == []
    assert api_b.get("/api/v1/rules/").data == []
    assert api_b.get("/api/v1/tags/").data == []
    assert api_b.get("/api/v1/transactions/").data["count"] == 0
    assert api_b.get("/api/v1/accounts/").data == {"items": [], "accounts": []}
    assert api_b.get("/api/v1/notifications/").data == []
    names = [c["name"] for c in api_b.get("/api/v1/categories/").data]
    assert "My cat" not in names and "Sales Revenue" in names  # system chart is shared, user categories are not


def test_detail_endpoints_return_404_for_other_users(api_b, data_a):
    d = data_a
    assert api_b.get(f"/api/v1/ledgers/{d['ledger'].id}/").status_code == 404
    assert api_b.patch(f"/api/v1/ledgers/{d['ledger'].id}/", {"label": "x"}, format="json").status_code == 404
    assert api_b.delete(f"/api/v1/ledgers/{d['ledger'].id}/").status_code == 404
    assert api_b.patch(f"/api/v1/rules/{d['rule'].id}/", {"active": False}, format="json").status_code == 404
    assert api_b.delete(f"/api/v1/rules/{d['rule'].id}/").status_code == 404
    assert api_b.patch(f"/api/v1/categories/{d['cat'].id}/", {"name": "x"}, format="json").status_code == 404
    assert api_b.delete(f"/api/v1/categories/{d['cat'].id}/").status_code == 404
    assert api_b.patch(f"/api/v1/transactions/{d['tx'].id}/", {"is_hidden": True}, format="json").status_code == 404
    assert api_b.patch(f"/api/v1/entries/{d['tx'].id}/", {}, format="json").status_code in (400, 404)
    assert api_b.delete(f"/api/v1/entries/{d['tx'].id}/").status_code == 404
    assert api_b.get(f"/api/v1/ledgers/{d['ledger'].id}/vendor-profile/?year=2026").status_code == 404
    assert api_b.delete(f"/api/v1/plaid/items/{d['item'].id}/").status_code == 404
    assert api_b.post(f"/api/v1/plaid/items/{d['item'].id}/refresh/").status_code == 404
    assert api_b.post("/api/v1/plaid/link-token/", {"item_id": str(d["item"].id)}, format="json").status_code == 404
    assert api_b.delete(f"/api/v1/ledgers/{d['ledger'].id}/tags/{d['tag'].id}/").status_code == 404
    assert Ledger.objects.filter(pk=d["ledger"].id).exists() and Transaction.objects.filter(pk=d["tx"].id).exists()


def test_cannot_attach_other_users_objects(api_b, user_b, data_a):
    d = data_a
    own = Transaction.objects.create(user=user_b, amount=5, transaction_date=datetime.date(2026, 1, 1))
    # B cannot assign their transaction to A's ledger or categorize with A's category
    assert api_b.patch(f"/api/v1/transactions/{own.id}/", {"ledger_id": str(d["ledger"].id)}, format="json").status_code == 400
    assert api_b.patch(f"/api/v1/transactions/{own.id}/", {"category_id": str(d["cat"].id)}, format="json").status_code == 400
    assert (
        api_b.post(
            "/api/v1/transactions/bulk-categorize/", {"ids": [str(own.id)], "category_id": str(d["cat"].id)}, format="json"
        ).status_code
        == 400
    )
    assert api_b.post("/api/v1/rules/", {"keyword": "zzz", "category_id": str(d["cat"].id)}, format="json").status_code == 400
    assert (
        api_b.post(
            "/api/v1/entries/",
            {"ledger_id": str(d["ledger"].id), "date": "2026-01-01", "description": "x", "amount": "5", "type": "revenue"},
            format="json",
        ).status_code
        == 400
    )
    # bulk operations silently skip rows B does not own
    api_b.post("/api/v1/transactions/assign-ledger/", {"ids": [str(d["tx"].id)], "ledger_id": None}, format="json")
    d["tx"].refresh_from_db()
    assert d["tx"].ledger_id == d["ledger"].id


def test_system_categories_are_read_only(api_a):
    sys = Category.objects.filter(user__isnull=True).first()
    assert api_a.patch(f"/api/v1/categories/{sys.id}/", {"name": "hacked"}, format="json").status_code == 404
    assert api_a.delete(f"/api/v1/categories/{sys.id}/").status_code == 404


def test_secrets_never_serialized(api_a, data_a):
    body = str(api_a.get("/api/v1/accounts/").data)
    assert "access_token" not in body and "sync_cursor" not in body
    data_a["ledger"].vendor_profile.tin_encrypted = encrypt_secret("123456789")
    data_a["ledger"].vendor_profile.save()
    r = api_a.get(f"/api/v1/ledgers/{data_a['ledger'].id}/vendor-profile/?year=2026")
    assert "tin_encrypted" not in str(r.data) and "123456789" not in str(r.data)
