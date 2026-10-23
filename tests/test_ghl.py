import hashlib
import hmac
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.crypto import encrypt_secret
from apps.ghl.models import GhlInvoice, GhlInvoicePayment, GhlLocation, GhlOAuthToken
from apps.ghl.services import invoices as svc
from apps.ghl.services import oauth
from apps.ledger.models import Ledger, Transaction

pytestmark = pytest.mark.django_db

INV = {
    "_id": "inv123456",
    "altId": "LOC1",
    "status": "partially_paid",
    "total": 110,
    "amountPaid": 50,
    "currency": "USD",
    "invoiceNumberPrefix": "INV-",
    "invoiceNumber": 7,
    "issueDate": "2026-03-01T00:00:00Z",
    "dueDate": "2026-03-31T00:00:00Z",
    "totalSummary": {"subTotal": 100, "discount": 0, "tax": 10},
    "contactDetails": {"id": "contact1", "name": "Jane Doe", "email": "jane@x.com", "companyName": "Jane Co"},
}
PAYS = {
    "data": [{"_id": "pay1", "status": "succeeded", "amount": 50, "amountRefunded": 0, "fee": 1.75, "createdAt": "2026-03-05T10:00:00Z"}]
}


def test_compute_invoice_statuses():
    c = svc.compute_invoice(INV, [{"amount": 50, "refunded": 0}])
    assert (c["status"], c["balance"], c["revenue"], c["tax"], c["reasons"]) == ("partially_paid", 60, 100, 10, [])
    assert svc.compute_invoice({**INV, "status": "paid", "amountPaid": 110})["status"] == "paid"
    assert svc.compute_invoice({**INV, "status": "void"})["status"] == "void"
    assert svc.compute_invoice({**INV, "amountPaid": 0, "dueDate": "2000-01-01"})["status"] == "overdue"
    c = svc.compute_invoice({**INV, "status": "refunded", "amountPaid": 110}, [{"amount": 110, "refunded": 110}])
    assert c["status"] == "refunded" and any("Refund" in r for r in c["reasons"])
    assert any("EUR" in r for r in svc.compute_invoice({**INV, "currency": "EUR"})["reasons"])
    assert svc.compute_invoice({"total": 10})["subtotal"] == 0
    assert svc.n("abc") == 0 and svc.n(None) == 0 and svc.n(0.125) == 0.13  # half-up like JS Math.round (banker's rounding would give 0.12)


@pytest.fixture
def fake_ghl(monkeypatch):
    def fake_get(location_id, path):
        if path.startswith("/payments/transactions"):
            return PAYS
        if path.startswith("/invoices/?"):
            return {"invoices": [INV], "total": 1}
        return {"invoice": INV}

    monkeypatch.setattr(svc, "ghl_get", fake_get)


def test_sync_invoice_posts_accrual_entries_idempotently(user_a, fake_ghl):
    GhlLocation.objects.create(location_id="LOC1", user=user_a)
    assert svc.sync_invoice("LOC1", user_a, "inv123456", "webhook", "InvoiceUpdate")["status"] == "partially_paid"
    svc.sync_invoice("LOC1", user_a, "inv123456", "webhook", "InvoiceUpdate")  # replay
    inv = GhlInvoice.objects.get()
    assert (inv.total, inv.balance, inv.amount_paid, inv.invoice_number) == (110, 60, 50, "INV-7")
    assert GhlInvoicePayment.objects.count() == 1
    ledger = Ledger.objects.get(ghl_contact_id="contact1")
    assert ledger.label == "Jane Co" and ledger.account_class == "current_asset" and inv.ledger == ledger

    refs = {t.external_ref: t for t in Transaction.objects.filter(user=user_a)}
    assert set(refs) == {"ghl:inv:inv123456:revenue", "ghl:inv:inv123456:ar", "ghl:inv:inv123456:tax", "ghl:pay:pay1", "ghl:fee:pay1"}
    assert refs["ghl:inv:inv123456:revenue"].amount == 100 and refs["ghl:inv:inv123456:revenue"].is_income
    ar = refs["ghl:inv:inv123456:ar"]
    assert (ar.amount, ar.ledger_id, ar.accounting_status) == (60, ledger.id, "accounts_receivable")
    assert refs["ghl:inv:inv123456:tax"].gaap_type == "liability"
    assert refs["ghl:fee:pay1"].amount == -1.75 and refs["ghl:fee:pay1"].is_expense
    assert refs["ghl:pay:pay1"].accounting_status == "received"
    # replay created no duplicate event
    assert inv.events.count() == 1


def test_sync_invoice_rejects_foreign_or_draft_invoices(user_a, monkeypatch):
    monkeypatch.setattr(svc, "ghl_get", lambda l, p: {"invoice": {**INV, "altId": "OTHER"}})
    with pytest.raises(RuntimeError):
        svc.sync_invoice("LOC1", user_a, "inv123456", "webhook")
    monkeypatch.setattr(svc, "ghl_get", lambda l, p: {"invoice": {**INV, "status": "draft"}})
    assert svc.sync_invoice("LOC1", user_a, "inv123456", "webhook") == {"skipped": True}
    assert not Transaction.objects.exists()


# --------------------------------------------------------------- sign-in
def test_session_creates_tenant_user_and_is_isolated(monkeypatch, fake_ghl):
    monkeypatch.setattr(oauth, "get_location_access_token", lambda l: "tok")
    c = APIClient()
    r = c.post("/api/v1/workspace/session/", {"id": "LOC1"}, format="json")
    assert r.status_code == 200 and r.data["access"]
    loc = GhlLocation.objects.get(location_id="LOC1")
    assert loc.user.email == "ghl-loc1@tenants.invalid" and not loc.user.has_usable_password()
    assert loc.invoices_synced_at and Transaction.objects.filter(user=loc.user).count() == 5  # first visit imports invoices
    again = c.post("/api/v1/workspace/session/", {"id": "LOC1"}, format="json")
    assert again.data["user_id"] == r.data["user_id"]
    other = c.post("/api/v1/workspace/session/", {"id": "LOC2"}, format="json")
    assert other.data["user_id"] != r.data["user_id"]
    # tenant A's token cannot read tenant B's data
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {other.data['access']}")
    assert c.get("/api/v1/transactions/").data["count"] == 0
    # password login is impossible for tenant users
    assert (
        APIClient().post("/api/v1/auth/login/", {"email": "ghl-loc1@tenants.invalid", "password": "guess"}, format="json").status_code
        == 401
    )


def test_session_validation_and_deactivation(monkeypatch):
    monkeypatch.setattr(oauth, "get_location_access_token", lambda l: "tok")
    monkeypatch.setattr(svc, "sync_location_invoices", lambda *a: {})
    c = APIClient()
    assert c.post("/api/v1/workspace/session/", {"id": "bad id!"}, format="json").status_code == 403
    assert c.post("/api/v1/workspace/session/", {}, format="json").status_code == 403
    c.post("/api/v1/workspace/session/", {"id": "LOC1"}, format="json")
    GhlLocation.objects.filter(location_id="LOC1").update(active=False)
    assert c.post("/api/v1/workspace/session/", {"id": "LOC1"}, format="json").status_code == 403


def test_optional_link_signature(monkeypatch, settings):
    monkeypatch.setattr(oauth, "get_location_access_token", lambda l: "tok")
    monkeypatch.setattr(svc, "sync_location_invoices", lambda *a: {})
    settings.GHL_LINK_SECRET = "s3cret"
    c = APIClient()
    assert c.post("/api/v1/workspace/session/", {"id": "LOC1"}, format="json").status_code == 403
    assert c.post("/api/v1/workspace/session/", {"id": "LOC1", "sig": "wrong"}, format="json").status_code == 403
    sig = hmac.new(b"s3cret", b"LOC1", hashlib.sha256).hexdigest()
    assert c.post("/api/v1/workspace/session/", {"id": "LOC1", "sig": sig}, format="json").status_code == 200


# --------------------------------------------------------------- webhook / cron
def test_webhook_invoice_event_resyncs_from_ghl_not_payload(user_a, fake_ghl):
    GhlLocation.objects.create(location_id="LOC1", user=user_a)
    c = APIClient()
    # payload claims it is fully paid; we must trust only what GoHighLevel returns
    r = c.post(
        "/api/v1/workspace/webhook/",
        {"type": "InvoicePaid", "locationId": "LOC1", "_id": "inv123456", "status": "paid", "total": 1},
        format="json",
    )
    assert r.status_code == 200 and GhlInvoice.objects.get().status == "partially_paid"
    assert c.post("/api/v1/workspace/webhook/", {"locationId": "LOC1"}, format="json").status_code == 400
    assert (
        c.post("/api/v1/workspace/webhook/", {"type": "InvoicePaid", "locationId": "LOC1", "_id": "../x"}, format="json").status_code == 200
    )  # bad id ignored
    assert GhlInvoice.objects.count() == 1


def test_webhook_failure_requests_retry(user_a, monkeypatch):
    GhlLocation.objects.create(location_id="LOC1", user=user_a)
    monkeypatch.setattr(svc, "ghl_get", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    r = APIClient().post("/api/v1/workspace/webhook/", {"type": "InvoiceCreate", "locationId": "LOC1", "_id": "inv123456"}, format="json")
    assert r.status_code == 500


def test_cron_endpoint_requires_secret():
    c = APIClient()
    assert c.post("/api/v1/workspace/refresh-tokens/").status_code == 401
    assert c.post("/api/v1/workspace/refresh-tokens/", HTTP_AUTHORIZATION="Bearer wrong").status_code == 401
    assert c.post("/api/v1/workspace/refresh-tokens/", HTTP_AUTHORIZATION="Bearer cron-secret").status_code == 200


def test_oauth_callback(monkeypatch, settings, fake_location_api):
    from urllib.parse import parse_qs, urlparse

    from apps.ghl.services import linking

    settings.FRONTEND_URL = "https://app.example.com"
    monkeypatch.setattr(
        oauth,
        "_token_request",
        lambda p: {
            "access_token": "a",
            "refresh_token": "r",
            "expires_in": 60,
            "locationId": "LOC9",
            "companyId": "C",
            "userType": "Location",
        },
    )
    c = APIClient()
    assert c.get("/api/v1/workspace/oauth/callback/?code=x").status_code == 400
    r = c.get("/api/v1/workspace/oauth/callback/?code=abcdefghijk")
    assert r.status_code == 302 and r["Location"].startswith("https://app.example.com/?id=LOC9&link=")
    q = parse_qs(urlparse(r["Location"]).query)
    assert q["id"] == ["LOC9"] and linking.read_link_code(q["link"][0]) == {"loc": "LOC9", "co": "C"}
    assert GhlOAuthToken.objects.filter(location_id="LOC9").exists()


def test_sync_invoices_endpoint_and_ledger_invoices(api_a, user_a, fake_ghl):
    assert api_a.post("/api/v1/workspace/invoices/sync/").status_code == 400  # workspace not linked
    GhlLocation.objects.create(location_id="LOC1", user=user_a)
    assert api_a.post("/api/v1/workspace/invoices/sync/").data == {"synced": 1, "failed": 0}
    ledger = Ledger.objects.get(ghl_contact_id="contact1")
    rows = api_a.get(f"/api/v1/workspace/ledgers/{ledger.id}/invoices/").data["invoices"]
    assert rows[0]["invoice_number"] == "INV-7" and float(rows[0]["balance"]) == 60


def test_agency_token_row_is_unique_even_with_null_location():
    from django.db import IntegrityError, transaction

    kw = {
        "company_id": "C1",
        "user_type": "Company",
        "access_token_encrypted": encrypt_secret("a"),
        "refresh_token_encrypted": encrypt_secret("r"),
        "expires_at": timezone.now() + timedelta(days=1),
    }
    GhlOAuthToken.objects.create(**kw)
    with pytest.raises(IntegrityError), transaction.atomic():
        GhlOAuthToken.objects.create(**kw)
    # a different company or a concrete location is fine
    GhlOAuthToken.objects.create(**{**kw, "company_id": "C2"})
    GhlOAuthToken.objects.create(**{**kw, "location_id": "LOCA"})


def test_install_redirects_to_consent_screen_built_from_settings(settings):
    from urllib.parse import parse_qs, urlparse

    settings.GHL_CLIENT_ID = "abc123-xyz"
    settings.GHL_VERSION_ID = "abc123"
    settings.GHL_REDIRECT_URI = "http://localhost:8000/api/v1/workspace/oauth/callback/"
    settings.GHL_SCOPES = ["invoices.readonly", "payments/transactions.readonly", "oauth.write"]
    r = APIClient().get("/api/v1/workspace/install/")  # public: no auth needed
    assert r.status_code == 302
    loc = r["Location"]
    assert loc.startswith(
        "https://marketplace.gohighlevel.com/v2/oauth/chooselocation?response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A8000"
    )
    assert "scope=invoices.readonly+payments%2Ftransactions.readonly+oauth.write" in loc  # same encoding GHL's own link uses
    q = parse_qs(urlparse(loc).query)
    assert q["client_id"] == ["abc123-xyz"] and q["version_id"] == ["abc123"]
    assert q["redirect_uri"] == ["http://localhost:8000/api/v1/workspace/oauth/callback/"]
    assert q["scope"][0].split() == settings.GHL_SCOPES
    assert "state" not in q
    assert parse_qs(urlparse(APIClient().get("/api/v1/workspace/install/?state=t1")["Location"]).query)["state"] == ["t1"]


def test_install_reports_missing_configuration(settings):
    settings.GHL_CLIENT_ID = ""
    r = APIClient().get("/api/v1/workspace/install/")
    assert r.status_code == 503 and r.data["error"]["code"] == "not_configured"
    assert "GHL_CLIENT_ID" not in str(r.data)  # config details stay in the server log


# ------------------------------------------------------------ location records (onboarding)
LOCATION_PAYLOAD = {"location": {"id": "LOC7", "name": "Acme Plumbing", "email": "hi@acme.co", "phone": "+15551234", "companyId": "CO1"}}


@pytest.fixture
def fake_location_api(monkeypatch):
    from apps.ghl.services import locations as loc

    monkeypatch.setattr(loc, "ghl_get", lambda lid, path: LOCATION_PAYLOAD if path == f"/locations/{lid}" else {})
    return loc


def test_location_install_records_id_and_name(fake_location_api, monkeypatch):
    monkeypatch.setattr(
        oauth,
        "_token_request",
        lambda p: {
            "access_token": "a",
            "refresh_token": "r",
            "expires_in": 60,
            "locationId": "LOC7",
            "companyId": "CO1",
            "userType": "Location",
        },
    )
    r = APIClient().get("/api/v1/workspace/oauth/callback/?code=abcdefghijk")
    assert r.status_code == 302
    loc = GhlLocation.objects.get(location_id="LOC7")
    assert (loc.name, loc.email, loc.phone, loc.company_id) == ("Acme Plumbing", "hi@acme.co", "+15551234", "CO1")
    assert loc.details["name"] == "Acme Plumbing" and loc.details_synced_at and loc.active and loc.user is None


def test_recording_is_idempotent_and_never_resets_user_or_active(fake_location_api, user_a):
    GhlLocation.objects.create(location_id="LOC7", user=user_a, active=False)
    fake_location_api.record_location("LOC7", company_id="CO1", name="Renamed")
    fake_location_api.record_location("LOC7")
    loc = GhlLocation.objects.get(location_id="LOC7")
    assert loc.user == user_a and loc.active is False and loc.name == "Renamed" and GhlLocation.objects.count() == 1


def test_install_webhook_only_keeps_locations_ghl_confirms(fake_location_api, monkeypatch):
    c = APIClient()
    c.post("/api/v1/workspace/webhook/", {"type": "INSTALL", "locationId": "LOC7", "companyId": "CO1"}, format="json")
    assert GhlLocation.objects.get(location_id="LOC7").name == "Acme Plumbing"
    # a forged id that GoHighLevel does not recognise leaves no junk row behind
    monkeypatch.setattr(fake_location_api, "ghl_get", lambda *a: (_ for _ in ()).throw(RuntimeError("404")))
    assert c.post("/api/v1/workspace/webhook/", {"type": "INSTALL", "locationId": "FAKE1"}, format="json").status_code == 200
    assert not GhlLocation.objects.filter(location_id="FAKE1").exists()
    assert c.post("/api/v1/workspace/webhook/", {"type": "INSTALL", "locationId": "bad id!"}, format="json").status_code == 200
    assert GhlLocation.objects.count() == 1


def test_first_sign_in_fills_in_the_name(fake_location_api, monkeypatch):
    monkeypatch.setattr(oauth, "get_location_access_token", lambda l: "tok")
    monkeypatch.setattr(svc, "sync_location_invoices", lambda *a: {})
    r = APIClient().post("/api/v1/workspace/session/", {"id": "LOC7"}, format="json")
    assert r.status_code == 200
    loc = GhlLocation.objects.get(location_id="LOC7")
    assert loc.name == "Acme Plumbing" and loc.user_id


def test_sign_in_succeeds_even_when_details_cannot_be_read(monkeypatch):
    from apps.ghl.services import locations as loc

    monkeypatch.setattr(oauth, "get_location_access_token", lambda l: "tok")
    monkeypatch.setattr(svc, "sync_location_invoices", lambda *a: {})
    monkeypatch.setattr(loc, "ghl_get", lambda *a: (_ for _ in ()).throw(RuntimeError("scope missing")))
    assert APIClient().post("/api/v1/workspace/session/", {"id": "LOC8"}, format="json").status_code == 200
    assert GhlLocation.objects.get(location_id="LOC8").name is None


def test_admin_actions_refresh_and_import(fake_location_api, monkeypatch):
    from django.contrib.auth import get_user_model
    from django.test import Client

    admin_user = get_user_model().objects.create_superuser("owner@example.com", "Adm1n-pass!")
    c = Client()
    c.force_login(admin_user)
    loc = GhlLocation.objects.create(location_id="LOC7")
    r = c.post("/admin/ghl/ghllocation/", {"action": "refresh_from_ghl", "_selected_action": [str(loc.pk)]}, follow=True)
    assert r.status_code == 200 and "Refreshed 1 location" in r.content.decode()
    loc.refresh_from_db()
    assert loc.name == "Acme Plumbing"
    monkeypatch.setattr("apps.ghl.admin.sync_installed_locations", lambda: {"locations": 3})
    r = c.post("/admin/ghl/ghllocation/", {"action": "sync_installed", "_selected_action": [str(loc.pk)]}, follow=True)
    assert "Imported 3 installed location" in r.content.decode()
    assert "Acme Plumbing" in c.get("/admin/ghl/ghllocation/").content.decode()  # name is visible in the list
