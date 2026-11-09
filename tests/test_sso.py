"""GoHighLevel SSO sign-in, automatic roles, per-user overrides, read-only enforcement, switcher and activity log."""

import pytest
from rest_framework.test import APIClient

from apps.core.models import AuditLog
from apps.ghl.models import GhlLocation, GhlMember
from apps.ghl.services import invoices as inv_svc
from apps.ghl.services import oauth
from apps.ghl.services import sso as sso_svc
from apps.ledger.models import Transaction

pytestmark = pytest.mark.django_db
SECRET = "shared-secret-from-marketplace"


@pytest.fixture(autouse=True)
def _sso(settings, monkeypatch):
    settings.GHL_SSO_SECRET = SECRET
    monkeypatch.setattr(oauth, "get_location_access_token", lambda l: "tok")
    monkeypatch.setattr(inv_svc, "sync_location_invoices", lambda *a: {"synced": 0, "failed": 0})
    monkeypatch.setattr(inv_svc, "ghl_get", lambda l, p: (_ for _ in ()).throw(RuntimeError("no users scope")))
    from apps.ghl.services import locations as loc_svc

    monkeypatch.setattr(loc_svc, "refresh_location_details", lambda loc: loc)


def payload(**over):
    data = {
        "userId": "U1",
        "companyId": "CO1",
        "role": "admin",
        "type": "location",
        "activeLocation": "LOC1",
        "userName": "Pat Admin",
        "email": "Pat@Example.com",
    }
    data.update(over)
    return sso_svc.encrypt_user_data(data, SECRET)


def sso(**over):
    body = {"payload": payload(**{k: v for k, v in over.items() if k != "id"})}
    if "id" in over:
        body["id"] = over["id"]
    return APIClient().post("/api/v1/workspace/sso/", body, format="json")


def as_token(access):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return c


def test_cryptojs_round_trip_and_bad_inputs():
    obj = {"userId": "U1", "companyId": "CO1", "role": "admin", "type": "agency", "userName": "Ann", "email": "a@b.c"}
    assert sso_svc.decrypt_user_data(sso_svc.encrypt_user_data(obj, SECRET), SECRET) == obj
    with pytest.raises(ValueError):
        sso_svc.decrypt_user_data(sso_svc.encrypt_user_data(obj, SECRET), "wrong")
    with pytest.raises(ValueError):
        sso_svc.decrypt_user_data("not base64!!", SECRET)
    with pytest.raises(ValueError):
        sso_svc.parse_user({"role": "admin"})  # no user / company
    with pytest.raises(ValueError):
        sso_svc.parse_user({"userId": "U1", "companyId": "C", "type": "alien"})


def test_bare_link_is_closed_once_sso_is_configured():
    r = APIClient().post("/api/v1/workspace/session/", {"id": "LOC1"}, format="json")
    assert r.status_code == 403 and r.data["error"]["code"] == "sso_required"


def test_sub_account_admin_signs_in_as_editor_and_is_recorded():
    r = sso()
    assert r.status_code == 200, r.data
    assert r.data["me"]["access"] == "editor" and r.data["me"]["can_edit"] and not r.data["me"]["can_manage_users"]
    assert r.data["me"]["actor"] == {"id": "U1", "name": "Pat Admin", "email": "pat@example.com", "type": "location", "role": "admin"}
    loc = GhlLocation.objects.get(location_id="LOC1")
    assert loc.company_id == "CO1" and loc.user.email == "ghl-loc1@tenants.invalid"
    m = GhlMember.objects.get(location=loc, ghl_user_id="U1")
    assert m.email == "pat@example.com" and m.access is None and m.last_seen_at
    log = AuditLog.objects.get(action="ghl_sso_sign_in")
    assert log.actor_id == "U1" and log.actor_name == "Pat Admin" and log.user == loc.user

    c = as_token(r.data["access"])
    assert c.post("/api/v1/transactions/recategorize/").status_code == 200  # editors write
    assert c.get("/api/v1/workspace/members/").status_code == 403  # but do not manage people
    me = c.get("/api/v1/workspace/me/").data
    assert me["access"] == "editor" and me["location"]["location_id"] == "LOC1" and not me["can_switch"]


def test_sub_account_user_has_no_access_until_switched_on():
    r = sso(userId="U2", role="user", userName="Sam Staff")
    assert r.status_code == 403 and r.data["error"]["code"] == "no_access"
    assert AuditLog.objects.filter(action="ghl_sso_denied", actor_id="U2").exists()
    loc = GhlLocation.objects.get(location_id="LOC1")
    GhlMember.objects.filter(location=loc, ghl_user_id="U2").update(access="viewer")
    r = sso(userId="U2", role="user", userName="Sam Staff")
    assert r.status_code == 200 and r.data["me"]["access"] == "viewer" and not r.data["me"]["can_edit"]


def test_viewer_can_read_but_not_write_and_can_sign_out():
    sso()  # creates LOC1
    loc = GhlLocation.objects.get(location_id="LOC1")
    GhlMember.objects.create(location=loc, ghl_user_id="U3", access="viewer")
    Transaction.objects.create(user=loc.user, amount=-5, transaction_date="2026-03-01", merchant_name="x")
    r = sso(userId="U3", role="user", userName="Vic Viewer")
    c = as_token(r.data["access"])
    assert c.get("/api/v1/transactions/").data["count"] == 1
    w = c.post("/api/v1/transactions/recategorize/")
    assert w.status_code == 403 and "view-only" in w.data["error"]["message"]
    assert c.post("/api/v1/plaid/sync/").status_code == 403
    assert c.post("/api/v1/auth/logout/").status_code == 204


def test_access_change_applies_to_live_sessions_immediately():
    editor = as_token(sso().data["access"])
    assert editor.post("/api/v1/transactions/recategorize/").status_code == 200
    loc = GhlLocation.objects.get(location_id="LOC1")
    GhlMember.objects.filter(location=loc, ghl_user_id="U1").update(access="viewer")
    assert editor.post("/api/v1/transactions/recategorize/").status_code == 403
    GhlMember.objects.filter(location=loc, ghl_user_id="U1").update(access="none")
    assert editor.get("/api/v1/transactions/").status_code == 403


def test_agency_user_is_admin_manages_members_and_sees_activity():
    sso()  # Pat (sub-account admin) signed in once
    r = sso(userId="A1", type="agency", role="user", userName="Agency Ann", email="ann@agency.com")
    assert r.status_code == 200 and r.data["me"]["access"] == "admin" and r.data["me"]["can_manage_users"] and r.data["me"]["can_switch"]
    admin = as_token(r.data["access"])

    members = admin.get("/api/v1/workspace/members/")
    assert members.status_code == 200
    by_id = {m["ghl_user_id"]: m for m in members.data["members"]}
    assert by_id["U1"]["effective_access"] == "editor" and by_id["U1"]["access"] is None
    assert by_id["A1"]["is_you"] and by_id["A1"]["effective_access"] == "admin"
    assert members.data["warnings"]  # users.readonly scope missing: list comes from sign-ins only

    # turn Pat down to viewer, then a brand-new user (never signed in) up to editor
    r = admin.put("/api/v1/workspace/members/LOC1/U1/", {"access": "viewer"}, format="json")
    assert r.status_code == 200 and r.data["effective_access"] == "viewer" and r.data["access_set_by"] == "Agency Ann"
    r = admin.put(
        "/api/v1/workspace/members/LOC1/NEW9/",
        {"access": "editor", "name": "New Person", "email": "new@x.com", "ghl_type": "location", "ghl_role": "user"},
        format="json",
    )
    assert r.status_code == 200 and r.data["name"] == "New Person"
    assert sso(userId="NEW9", role="user").data["me"]["access"] == "editor"
    # back to automatic
    r = admin.put("/api/v1/workspace/members/LOC1/U1/", {"access": None}, format="json")
    assert r.data["access"] is None and r.data["effective_access"] == "editor"
    # guard rails
    assert admin.put("/api/v1/workspace/members/LOC1/A1/", {"access": "none"}, format="json").status_code == 400
    assert admin.put("/api/v1/workspace/members/LOC1/U1/", {"access": "god"}, format="json").status_code == 400
    assert admin.put("/api/v1/workspace/members/OTHER/U1/", {"access": "none"}, format="json").status_code == 404

    act = admin.get("/api/v1/workspace/activity/", {"actor": "A1"})
    assert act.status_code == 200
    actions = [row["action"] for row in act.data["rows"]]
    assert actions.count("member_access_changed") == 3 and all(row["actor"]["name"] == "Agency Ann" for row in act.data["rows"])
    assert {a["id"] for a in act.data["actors"]} >= {"A1", "U1"}
    assert admin.get("/api/v1/workspace/activity/", {"q": "sso_sign_in"}).data["count"] >= 2


def test_agency_switcher_lists_own_agency_and_opens_other_sub_accounts():
    GhlLocation.objects.create(location_id="LOC2", name="Second Client", company_id="CO1")
    GhlLocation.objects.create(location_id="LOC9", name="Other agency", company_id="CO9")
    r = sso(userId="A1", type="agency", role="admin", userName="Agency Ann")
    admin = as_token(r.data["access"])
    locs = {x["location_id"]: x for x in admin.get("/api/v1/workspace/locations/").data["locations"]}
    assert set(locs) == {"LOC1", "LOC2"} and locs["LOC1"]["current"] and locs["LOC2"]["can_open"] and not locs["LOC2"]["current"]

    switched = sso(userId="A1", type="agency", role="admin", userName="Agency Ann", id="LOC2")
    assert switched.status_code == 200 and switched.data["me"]["location"]["location_id"] == "LOC2"
    assert switched.data["user_id"] != r.data["user_id"]
    assert sso(userId="A1", type="agency", role="admin", id="LOC9").status_code == 403  # another agency
    assert sso(id="LOC2").status_code == 403  # a sub-account admin cannot hop sub-accounts
    assert as_token(sso().data["access"]).get("/api/v1/workspace/locations/").status_code == 403


def test_personal_and_support_sessions_are_untouched(user_a, api_a):
    me = api_a.get("/api/v1/workspace/me/").data
    assert me["access"] == "owner" and me["can_edit"] and me["actor"] is None
    assert api_a.post("/api/v1/transactions/recategorize/").status_code == 200


def test_sso_endpoint_reports_missing_server_secret(settings):
    settings.GHL_SSO_SECRET = ""
    r = APIClient().post("/api/v1/workspace/sso/", {"payload": "anything"}, format="json")
    assert r.status_code == 503 and r.data["error"]["code"] == "not_configured"
    # and the bare link keeps working, so a deploy without the secret changes nothing for users
    assert APIClient().post("/api/v1/workspace/session/", {"id": "LOC1"}, format="json").status_code == 200
