"""Linking a GoHighLevel location to a signed-in (email) workspace."""

from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from rest_framework.test import APIClient

from apps.core.models import AuditLog
from apps.ghl import tasks
from apps.ghl.models import GhlLocation
from apps.ghl.services import invoices as inv_svc
from apps.ghl.services import linking, oauth
from apps.ledger.models import Ledger

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def imports(monkeypatch):
    """Capture the first-invoice-import instead of calling GoHighLevel."""
    calls = []
    monkeypatch.setattr(
        inv_svc, "sync_location_invoices", lambda lid, user: calls.append((lid, str(user.pk))) or {"synced": 0, "failed": 0}
    )
    return calls


def loc(location_id="LOC1", **kw):
    return GhlLocation.objects.create(location_id=location_id, name=kw.pop("name", "Acme"), company_id=kw.pop("company_id", "CO1"), **kw)


def code(location_id="LOC1", company_id="CO1"):
    return linking.make_link_code(location_id, company_id)


def test_code_is_signed_and_expires(monkeypatch):
    c = code()
    assert linking.read_link_code(c) == {"loc": "LOC1", "co": "CO1"}
    with pytest.raises(Exception) as e:
        linking.read_link_code(c + "x")
    assert "not valid" in str(e.value)
    assert linking.LINK_MAX_AGE == 900
    monkeypatch.setattr(linking, "LINK_MAX_AGE", -1)  # anything older than "now" is expired
    with pytest.raises(Exception) as e:
        linking.read_link_code(c)
    assert "expired" in str(e.value)


def test_link_requires_login_and_a_valid_code(api_a):
    assert APIClient().post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json").status_code == 401
    assert APIClient().get("/api/v1/workspace/links/").status_code == 401
    assert api_a.post("/api/v1/workspace/link/confirm/", {"code": "forged"}, format="json").status_code == 400
    assert api_a.post("/api/v1/workspace/link/confirm/", {}, format="json").status_code == 400
    assert (
        api_a.post(
            "/api/v1/workspace/link/confirm/", {"code": signing.dumps({"loc": "LOC1"}, salt="other-salt")}, format="json"
        ).status_code
        == 400
    )


def test_location_code_links_the_location_to_my_workspace_and_imports_invoices(api_a, user_a, imports):
    loc()
    opts = api_a.get(f"/api/v1/workspace/link/options/?code={code()}").data
    assert opts["choose"] is False and opts["locations"][0]["name"] == "Acme" and opts["locations"][0]["link_state"] == "free"
    r = api_a.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json")
    assert r.status_code == 200 and r.data["location_id"] == "LOC1" and r.data["link_state"] == "here"
    assert GhlLocation.objects.get(location_id="LOC1").user == user_a
    assert imports == [("LOC1", str(user_a.pk))]
    assert AuditLog.objects.filter(action="ghl_location_linked", user=user_a).exists()
    assert [x["location_id"] for x in api_a.get("/api/v1/workspace/links/").data["locations"]] == ["LOC1"]
    # the workspace's own "Sync invoices" now works because it is linked
    assert GhlLocation.objects.filter(user=user_a, active=True).exists()
    # confirming twice is harmless and does not re-import
    api_a.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json")
    assert len(imports) == 1


def test_company_code_lets_the_user_choose_among_installed_locations(api_a, user_a, imports):
    loc("A", name="Alpha")
    loc("B", name="Beta")
    loc("X", name="Other agency", company_id="CO2")
    c = code(None, "CO1")
    opts = api_a.get(f"/api/v1/workspace/link/options/?code={c}").data
    assert opts["choose"] is True and [x["name"] for x in opts["locations"]] == ["Alpha", "Beta"]  # only this company's
    assert api_a.post("/api/v1/workspace/link/confirm/", {"code": c}, format="json").status_code == 400  # must choose
    assert (
        api_a.post("/api/v1/workspace/link/confirm/", {"code": c, "location_id": "X"}, format="json").status_code == 400
    )  # not this company
    assert api_a.post("/api/v1/workspace/link/confirm/", {"code": c, "location_id": "B"}, format="json").status_code == 200
    assert GhlLocation.objects.get(location_id="B").user == user_a and GhlLocation.objects.get(location_id="A").user is None


def test_a_location_code_cannot_be_used_for_a_different_location(api_a, imports):
    loc("A")
    loc("B")
    api_a.post("/api/v1/workspace/link/confirm/", {"code": code("A"), "location_id": "B"}, format="json")
    assert GhlLocation.objects.get(location_id="B").user is None  # the code's own location wins; body cannot redirect it
    assert GhlLocation.objects.get(location_id="A").user is not None


def test_cannot_take_a_location_linked_to_another_workspace(api_a, api_b, user_b, imports):
    loc()
    api_b.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json")
    r = api_a.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json")
    assert r.status_code == 409 and "another workspace" in r.data["error"]["message"]
    assert GhlLocation.objects.get(location_id="LOC1").user == user_b
    assert api_a.get(f"/api/v1/workspace/link/options/?code={code()}").data["locations"][0]["link_state"] == "elsewhere"


def test_an_empty_hidden_tenant_can_be_handed_over_but_one_with_data_cannot(api_a, user_a, imports):
    tenant = User.objects.create_user("ghl-loc1@tenants.invalid", None)
    loc(user=tenant)
    assert api_a.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json").status_code == 200  # nothing to lose
    other = User.objects.create_user("ghl-loc2@tenants.invalid", None)
    loc("LOC2", user=other)
    Ledger.objects.create(user=other, label="Customer", account_class="current_asset")
    r = api_a.post("/api/v1/workspace/link/confirm/", {"code": code("LOC2")}, format="json")
    assert r.status_code == 409 and GhlLocation.objects.get(location_id="LOC2").user == other  # the tenant's books are protected


def test_inactive_locations_cannot_be_linked(api_a, imports):
    loc(active=False)
    assert api_a.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json").status_code == 409


def test_unlink_only_your_own_location(api_a, api_b, user_a, imports):
    loc()
    api_a.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json")
    assert api_b.delete("/api/v1/workspace/links/LOC1/").status_code == 404
    assert GhlLocation.objects.get(location_id="LOC1").user == user_a
    assert api_a.delete("/api/v1/workspace/links/LOC1/").status_code == 204
    assert GhlLocation.objects.get(location_id="LOC1").user is None
    assert api_a.get("/api/v1/workspace/links/").data["locations"] == []
    assert AuditLog.objects.filter(action="ghl_location_unlinked").exists()
    # unlinking never deletes the location, its tokens, or the workspace's data


def test_a_linked_personal_workspace_cannot_be_opened_with_just_a_location_id(api_a, user_a, imports):
    loc()
    api_a.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json")
    r = APIClient().post("/api/v1/workspace/session/", {"id": "LOC1"}, format="json")
    assert r.status_code == 403 and "personal workspace" in r.data["error"]["message"]
    assert "access" not in r.data


def test_embedded_sign_in_for_unlinked_locations_still_works(monkeypatch, imports):
    monkeypatch.setattr(oauth, "get_location_access_token", lambda l: "tok")
    r = APIClient().post("/api/v1/workspace/session/", {"id": "FRESH1"}, format="json")
    assert r.status_code == 200 and "access" in r.data


def test_webhook_invoices_land_in_the_linked_workspace(api_a, user_a, imports, monkeypatch):
    loc()
    api_a.post("/api/v1/workspace/link/confirm/", {"code": code()}, format="json")
    seen = []
    monkeypatch.setattr(inv_svc, "sync_invoice", lambda lid, user, iid, src, et=None, inv=None: seen.append((lid, user.pk, iid)))
    APIClient().post("/api/v1/workspace/webhook/", {"type": "InvoiceUpdate", "locationId": "LOC1", "_id": "inv123456"}, format="json")
    assert seen == [("LOC1", user_a.pk, "inv123456")]


def test_callback_hands_the_browser_a_link_code_for_company_installs(monkeypatch, settings):
    settings.FRONTEND_URL = "https://app.example.com"
    monkeypatch.setattr(
        oauth,
        "_token_request",
        lambda p: {"access_token": "a", "refresh_token": "r", "expires_in": 60, "companyId": "CO1", "userType": "Company"},
    )
    from apps.ghl.services import locations as loc_svc

    monkeypatch.setattr(loc_svc, "sync_installed_locations", lambda: {"locations": 0})
    r = APIClient().get("/api/v1/workspace/oauth/callback/?code=abcdefghijk")
    q = parse_qs(urlparse(r["Location"]).query)
    assert r["Location"].startswith("https://app.example.com/?link=") and "id" not in q
    assert linking.read_link_code(q["link"][0]) == {"loc": None, "co": "CO1"}


def test_import_task_runs_the_first_invoice_sync(user_a, imports):
    assert tasks.import_location_invoices_task("LOC1", str(user_a.pk)) == {"synced": 0, "failed": 0}
    assert imports == [("LOC1", str(user_a.pk))]
