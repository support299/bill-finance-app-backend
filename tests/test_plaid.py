import hashlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from rest_framework.test import APIClient

from apps.banking.models import BankAccount, PlaidItem, SyncLog
from apps.banking.services import plaid_client
from apps.core.crypto import decrypt_secret
from apps.core.models import Notification
from apps.ledger.models import Category, CategoryRule, Ledger, Transaction

pytestmark = pytest.mark.django_db

ACCOUNTS = {
    "accounts": [
        {
            "account_id": "chk1",
            "name": "Checking",
            "official_name": None,
            "mask": "0000",
            "type": "depository",
            "subtype": "checking",
            "balances": {"current": 1200.5, "available": 1100, "iso_currency_code": "USD"},
        },
        {
            "account_id": "cc1",
            "name": "Card",
            "official_name": None,
            "mask": "1111",
            "type": "credit",
            "subtype": "credit card",
            "balances": {"current": 300, "available": None, "iso_currency_code": "USD"},
        },
    ]
}


def ptx(tid, name, amount, date="2026-02-01", **kw):
    return {
        "transaction_id": tid,
        "account_id": "chk1",
        "amount": amount,
        "iso_currency_code": "USD",
        "unofficial_currency_code": None,
        "date": date,
        "authorized_date": None,
        "name": name,
        "merchant_name": kw.pop("merchant_name", name),
        "pending": False,
        "payment_channel": "online",
        "personal_finance_category": kw.pop("pfc", None),
        **kw,
    }


@pytest.fixture
def fake_plaid(monkeypatch):
    calls = []
    pages = [
        {
            "added": [
                ptx("t1", "Starbucks", 12.5),
                ptx("t2", "Stripe", -900, pfc={"primary": "INCOME", "detailed": "INCOME_OTHER_INCOME"}),
                ptx("t3", "Old thing", 5),
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "c1",
            "has_more": True,
        },
        {
            "added": [],
            "modified": [ptx("t1", "Starbucks", 13.0)],
            "removed": [{"transaction_id": "t3"}],
            "next_cursor": "c2",
            "has_more": False,
        },
    ]

    def fake_request(path, body):
        calls.append((path, body))
        if path == "/accounts/get":
            return ACCOUNTS
        if path == "/transactions/sync":
            return (
                pages.pop(0)
                if body.get("cursor") != "c2"
                else {"added": [], "modified": [], "removed": [], "next_cursor": "c2", "has_more": False}
            )
        if path == "/item/public_token/exchange":
            return {"access_token": "access-sandbox-abc", "item_id": "item-1"}
        if path == "/link/token/create":
            return {"link_token": "link-sandbox-xyz", "expiration": "x"}
        if path in ("/item/remove", "/transactions/refresh"):
            return {"request_id": "r"}
        if path == "/item/get":
            return {
                "item": {"item_id": "item-1", "institution_id": "ins", "error": {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "x"}}
            }
        raise AssertionError(path)

    monkeypatch.setattr(plaid_client, "plaid_request", fake_request)
    return calls


def connect(api):
    return api.post(
        "/api/v1/plaid/exchange/",
        {"public_token": "public-sandbox-123456", "institution_id": "ins_1", "institution_name": "Test Bank"},
        format="json",
    )


def test_exchange_stores_encrypted_token_and_syncs(api_a, user_a, fake_plaid):
    r = connect(api_a)
    assert r.status_code == 200, r.data
    assert (r.data["added"], r.data["modified"], r.data["removed"], r.data["status"]) == (3, 1, 1, "success")
    item = PlaidItem.objects.get(user=user_a)
    assert item.access_token_encrypted != "access-sandbox-abc" and decrypt_secret(item.access_token_encrypted) == "access-sandbox-abc"
    assert item.sync_cursor == "c2" and item.initial_sync_done and item.connection_status == "good"
    assert BankAccount.objects.filter(user=user_a).count() == 2
    log = SyncLog.objects.get(user=user_a)
    assert (log.status, log.transactions_added, log.transactions_removed, log.trigger_source) == ("success", 3, 1, "connect")
    assert Notification.objects.filter(user=user_a, title="Initial transaction import complete").exists()

    starbucks = Transaction.objects.get(plaid_transaction_id="t1")
    assert starbucks.amount == -13  # stored in account view: out is negative, and the modified page updated it
    assert starbucks.user_category.name == "Meals & Entertainment" and starbucks.is_expense and starbucks.source == "plaid"
    stripe = Transaction.objects.get(plaid_transaction_id="t2")
    assert stripe.amount == 900 and stripe.is_income
    assert Transaction.objects.get(plaid_transaction_id="t3").removed_at is not None  # soft-removed

    # secrets never appear in the API
    accounts = api_a.get("/api/v1/accounts/").data
    assert accounts["items"][0]["transaction_count"] == 2 and "access_token" not in str(accounts)


def test_sync_is_idempotent_and_rate_limited(api_a, fake_plaid):
    connect(api_a)
    r = api_a.post("/api/v1/plaid/sync/", {}, format="json")
    assert r.data["skipped"] == 1 and r.data["synced"] == 0  # synced less than a minute ago
    assert Transaction.objects.count() == 3


def test_sync_applies_ledger_rules_and_keeps_manual_ledger(api_a, user_a, fake_plaid):
    ledger = Ledger.objects.create(user=user_a, label="Coffee", account_class="current_liability")
    CategoryRule.objects.create(
        user=user_a, keyword="starbucks", ledger=ledger, category=Category.objects.get(user__isnull=True, name="Travel")
    )
    connect(api_a)
    t = Transaction.objects.get(plaid_transaction_id="t1")
    assert t.ledger_id == ledger.id and t.user_category.name == "Travel"
    # a ledger assigned by hand survives the next modified update
    other = Ledger.objects.create(user=user_a, label="Other", account_class="equity")
    t2 = Transaction.objects.get(plaid_transaction_id="t2")
    t2.ledger = other
    t2.save()
    from apps.banking.services.sync import _to_fields

    assert "ledger_id" not in _to_fields(ptx("t2", "Stripe", -900), PlaidItem.objects.get(), {}, [], [])


def test_sync_failure_marks_item_and_notifies(api_a, user_a, monkeypatch, fake_plaid):
    connect(api_a)
    item = PlaidItem.objects.get(user=user_a)
    item.last_sync_at = None
    item.save()

    def boom(path, body):
        raise plaid_client.PlaidError("x", "ITEM_LOGIN_REQUIRED", "ITEM_ERROR", 400)

    monkeypatch.setattr(plaid_client, "plaid_request", boom)
    r = api_a.post("/api/v1/plaid/sync/", {}, format="json")
    assert r.status_code == 200
    item.refresh_from_db()
    assert item.connection_status == "requires_reauth" and "sign in again" in item.error_message
    assert SyncLog.objects.filter(status="error").exists()
    assert Notification.objects.filter(user=user_a, type="error").exists()


def test_exchange_rejects_item_owned_by_someone_else(api_a, api_b, fake_plaid):
    connect(api_a)
    r = connect(api_b)
    assert r.status_code == 400 and "another account" in str(r.data)


def test_link_token_and_config(api_a, fake_plaid):
    assert api_a.post("/api/v1/plaid/link-token/", {}, format="json").data == {"link_token": "link-sandbox-xyz"}
    connect(api_a)
    item = PlaidItem.objects.get()
    assert api_a.post("/api/v1/plaid/link-token/", {"item_id": str(item.id)}, format="json").status_code == 200
    update_call = [b for p, b in fake_plaid if p == "/link/token/create"][-1]
    assert update_call["access_token"] == "access-sandbox-abc" and "products" not in update_call  # update mode
    assert api_a.get("/api/v1/plaid/config/").data["configured"] is True


def test_plaid_errors_are_friendly(api_a, monkeypatch):
    def boom(path, body):
        raise plaid_client.PlaidError("secret provider detail", "RATE_LIMIT_EXCEEDED", "X", 429)

    monkeypatch.setattr(plaid_client, "plaid_request", boom)
    r = api_a.post("/api/v1/plaid/link-token/", {}, format="json")
    assert r.status_code == 400 and "secret provider detail" not in str(r.data) and "Too many requests" in str(r.data)


def test_remove_item_keeps_or_deletes_history(api_a, user_a, fake_plaid):
    connect(api_a)
    item = PlaidItem.objects.get()
    assert api_a.delete(f"/api/v1/plaid/items/{item.id}/?delete_data=false").status_code == 204
    assert PlaidItem.objects.count() == 0 and Transaction.objects.filter(plaid_item__isnull=True).count() == 3  # history kept
    connect(api_a)
    item = PlaidItem.objects.get()
    api_a.delete(f"/api/v1/plaid/items/{item.id}/?delete_data=true")
    assert Transaction.objects.filter(plaid_item=item).count() == 0


# ------------------------------------------------------------------ webhook
@pytest.fixture
def signed(monkeypatch):
    priv = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key()))
    monkeypatch.setattr(plaid_client, "plaid_request", lambda p, b: {"key": jwk} if p == "/webhook_verification_key/get" else {})
    monkeypatch.setattr("apps.banking.webhooks.plaid.webhook_verification_key", lambda kid: {"key": jwk})

    def sign(body: bytes, iat=None, sha=None):
        return jwt.encode(
            {"iat": iat or int(time.time()), "request_body_sha256": sha or hashlib.sha256(body).hexdigest()},
            priv,
            algorithm="ES256",
            headers={"kid": "k1"},
        )

    return sign


def post_hook(body: dict, token):
    raw = json.dumps(body).encode()
    return APIClient().generic(
        "POST",
        "/api/v1/webhooks/plaid/",
        raw,
        content_type="application/json",
        **({"HTTP_PLAID_VERIFICATION": token(raw)} if callable(token) else ({"HTTP_PLAID_VERIFICATION": token} if token else {})),
    )


def test_webhook_rejects_unsigned_and_bad_signatures(signed, db):
    assert post_hook({"item_id": "x"}, None).status_code == 401
    assert post_hook({"item_id": "x"}, "garbage").status_code == 401
    assert post_hook({"item_id": "x"}, lambda raw: signed(raw, sha="0" * 64)).status_code == 401  # body tampered
    assert post_hook({"item_id": "x"}, lambda raw: signed(raw, iat=int(time.time()) - 3600)).status_code == 401  # stale token


def test_webhook_triggers_sync_and_error_handling(signed, api_a, user_a, fake_plaid):
    connect(api_a)
    item = PlaidItem.objects.get()
    before = SyncLog.objects.count()
    r = post_hook({"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item-1"}, signed)
    assert r.status_code == 200 and SyncLog.objects.count() == before + 1
    assert SyncLog.objects.order_by("-sync_started_at").first().trigger_source == "webhook"
    r = post_hook(
        {"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": "item-1", "error": {"error_code": "ITEM_LOGIN_REQUIRED"}}, signed
    )
    item.refresh_from_db()
    assert r.status_code == 200 and item.connection_status == "requires_reauth"
    assert Notification.objects.filter(title="Bank connection requires attention").exists()
    post_hook({"webhook_type": "ITEM", "webhook_code": "PENDING_EXPIRATION", "item_id": "item-1"}, signed)
    item.refresh_from_db()
    assert item.error_code == "PENDING_EXPIRATION"
    assert (
        post_hook({"webhook_type": "ITEM", "webhook_code": "X", "item_id": "unknown"}, signed).status_code == 200
    )  # unknown items ignored
