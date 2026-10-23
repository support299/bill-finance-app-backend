"""Agency token -> per-location tokens (GoHighLevel OAuth): minting at onboarding, correct token per call, refresh safety."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.crypto import decrypt_secret, encrypt_secret
from apps.ghl.models import GhlLocation, GhlOAuthToken
from apps.ghl.services import invoices as inv_svc
from apps.ghl.services import locations as loc_svc
from apps.ghl.services import oauth

pytestmark = pytest.mark.django_db


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    def json(self):
        return self._body


@pytest.fixture
def agency(settings):
    settings.GHL_APP_ID = "app1"
    return GhlOAuthToken.objects.create(
        company_id="CO1",
        user_type="Company",
        scope="invoices.readonly oauth.write",
        access_token_encrypted=encrypt_secret("AGENCY-AT"),
        refresh_token_encrypted=encrypt_secret("AGENCY-RT"),
        expires_at=timezone.now() + timedelta(hours=20),
    )


@pytest.fixture
def ghl_http(monkeypatch):
    """Fake GoHighLevel: records every call and answers the documented endpoints."""
    calls = []
    state = {"installed": [], "legacy_only": False, "mint_status": 200}

    def post(url, **kw):
        calls.append(("POST", url, kw))
        if "location-token" in url or "locationToken" in url:
            if state["legacy_only"] and "location-token" in url:
                return Resp(404, {"message": "Cannot POST"})
            if state["mint_status"] != 200:
                return Resp(state["mint_status"], {"message": "nope"})
            lid = kw["data"]["locationId"]
            return Resp(
                200,
                {
                    "access_token": f"LOC-AT-{lid}",
                    "refresh_token": f"LOC-RT-{lid}",
                    "expires_in": 86399,
                    "scope": "invoices.readonly",
                    "locationId": lid,
                    "userId": "u1",
                    "token_type": "Bearer",
                },
            )
        if url.endswith("/oauth/token"):
            d = kw["data"]
            return Resp(
                200,
                {
                    "access_token": "NEW-AT",
                    "refresh_token": "NEW-RT",
                    "expires_in": 86399,
                    "scope": "invoices.readonly",
                    "userType": d.get("user_type"),
                },
            )
        raise AssertionError(url)

    def get(url, **kw):
        calls.append(("GET", url, kw))
        if "installed-locations" in url:
            if state["legacy_only"]:
                return Resp(404, {"message": "Cannot GET"})
            items = state["installed"]
            size = kw["params"]["pageSize"]
            start = int(kw["params"].get("pageToken", 0))
            page = items[start : start + size]
            nxt = start + size
            return Resp(
                200,
                {"items": page, "pagination": {"hasNextPage": nxt < len(items), "nextPageToken": str(nxt) if nxt < len(items) else None}},
            )
        if "installedLocations" in url:
            items = state["installed"]
            skip, limit = kw["params"]["skip"], kw["params"]["limit"]
            return Resp(200, {"locations": items[skip : skip + limit], "count": len(items)})
        return Resp(
            200, {"location": {"id": url.rsplit("/", 1)[-1], "name": "Fetched Name", "companyId": "CO1"}, "invoices": [], "total": 0}
        )

    monkeypatch.setattr(oauth.httpx, "post", post)
    monkeypatch.setattr(oauth.httpx, "get", get)
    monkeypatch.setattr(loc_svc.httpx, "get", get)
    monkeypatch.setattr(inv_svc.httpx, "get", get)
    return calls, state


def posts(calls, needle):
    return [c for c in calls if c[0] == "POST" and needle in c[1]]


def test_minting_uses_the_v3_endpoint_with_the_agency_token(agency, ghl_http):
    calls, _ = ghl_http
    row = oauth.mint_location_token("LOC1")
    ((_, url, kw),) = posts(calls, "location-token")
    assert url.endswith("/oauth/location-token") and kw["headers"]["Version"] == "v3"
    assert kw["headers"]["Authorization"] == "Bearer AGENCY-AT"
    assert kw["data"] == {"companyId": "CO1", "locationId": "LOC1"}
    assert (row.user_type, row.company_id, row.location_id) == ("Location", "CO1", "LOC1")
    assert decrypt_secret(row.access_token_encrypted) == "LOC-AT-LOC1" and decrypt_secret(row.refresh_token_encrypted) == "LOC-RT-LOC1"
    assert row.minted_from_scope == agency.scope and row.expires_at > timezone.now() + timedelta(hours=23)
    assert GhlOAuthToken.objects.count() == 2  # the agency row is untouched, the location has its own


def test_minting_falls_back_to_the_legacy_endpoint_when_v3_is_not_served(agency, ghl_http):
    calls, state = ghl_http
    state["legacy_only"] = True
    oauth.mint_location_token("LOC1")
    urls = [c[1] for c in posts(calls, "ocation")]
    assert urls[0].endswith("/oauth/location-token") and urls[1].endswith("/oauth/locationToken")
    assert posts(calls, "locationToken")[0][2]["headers"]["Version"] == "2021-07-28"


def test_minting_errors_are_reported_not_swallowed(agency, ghl_http):
    _, state = ghl_http
    state["mint_status"] = 400
    with pytest.raises(oauth.GhlError) as e:
        oauth.mint_location_token("LOC1")
    assert e.value.status == 400
    assert not GhlOAuthToken.objects.filter(location_id="LOC1").exists()


def test_agency_install_gives_every_location_its_own_token_at_onboarding(agency, ghl_http):
    calls, state = ghl_http
    state["installed"] = [{"_id": f"L{i}", "name": f"Shop {i}", "isInstalled": True} for i in range(130)]  # two v3 pages
    assert loc_svc.sync_installed_locations() == {"locations": 130}
    assert GhlLocation.objects.count() == 130 and GhlLocation.objects.get(location_id="L7").name == "Shop 7"
    assert GhlOAuthToken.objects.filter(user_type="Location").count() == 130  # eager: no first-use needed
    row = GhlOAuthToken.objects.get(location_id="L7")
    assert decrypt_secret(row.access_token_encrypted) == "LOC-AT-L7"
    # the listing used the agency token and the documented v3 parameters
    listing = [c for c in calls if "installed-locations" in c[1]]
    assert len(listing) == 2 and listing[0][2]["headers"]["Version"] == "v3"
    assert listing[0][2]["params"] == {"companyId": "CO1", "appId": "app1", "isInstalled": "true", "pageSize": 100}
    assert listing[1][2]["params"]["pageToken"] == "100"
    assert listing[0][2]["headers"]["Authorization"] == "Bearer AGENCY-AT"
    # a second sync does not mint again
    before = len(posts(calls, "location-token"))
    loc_svc.sync_installed_locations()
    assert len(posts(calls, "location-token")) == before


def test_installed_locations_legacy_fallback(agency, ghl_http):
    _, state = ghl_http
    state["legacy_only"] = True
    state["installed"] = [{"_id": "A1", "name": "Alpha"}, {"_id": "A2", "name": "Beta"}]
    assert loc_svc.sync_installed_locations() == {"locations": 2}
    assert GhlLocation.objects.get(location_id="A2").name == "Beta"


def test_data_calls_use_the_location_token_never_the_agency_token(agency, ghl_http):
    calls, _ = ghl_http
    inv_svc.ghl_get("LOC1", "/invoices/?altId=LOC1&altType=location&limit=100&offset=0")
    loc_svc.refresh_location_details(GhlLocation.objects.create(location_id="LOC1"))
    gets = [c for c in calls if c[0] == "GET"]
    assert len(gets) == 2
    assert all(c[2]["headers"]["Authorization"] == "Bearer LOC-AT-LOC1" for c in gets)
    assert len(posts(calls, "location-token")) == 1  # minted once, then reused from storage


def test_location_token_is_reused_until_close_to_expiry_then_refreshed_once(agency, ghl_http):
    calls, _ = ghl_http
    assert oauth.get_location_access_token("LOC1") == "LOC-AT-LOC1"
    assert oauth.get_location_access_token("LOC1") == "LOC-AT-LOC1"
    assert not posts(calls, "/oauth/token")
    GhlOAuthToken.objects.filter(location_id="LOC1").update(expires_at=timezone.now() + timedelta(minutes=1))
    assert oauth.get_location_access_token("LOC1") == "NEW-AT"
    assert oauth.get_location_access_token("LOC1") == "NEW-AT"  # second call sees the fresh token: no second refresh
    refreshes = posts(calls, "/oauth/token")
    assert len(refreshes) == 1
    d = refreshes[0][2]["data"]
    assert d["grant_type"] == "refresh_token" and d["refresh_token"] == "LOC-RT-LOC1" and d["user_type"] == "Location"
    row = GhlOAuthToken.objects.get(location_id="LOC1")
    assert (
        decrypt_secret(row.refresh_token_encrypted) == "NEW-RT" and row.location_id == "LOC1" and row.company_id == "CO1"
    )  # rotation stored, ids kept


def test_agency_token_is_refreshed_with_company_user_type(agency, ghl_http):
    calls, _ = ghl_http
    GhlOAuthToken.objects.filter(pk=agency.pk).update(expires_at=timezone.now() + timedelta(minutes=2))
    token, company = oauth.get_agency_access_token()
    assert (token, company) == ("NEW-AT", "CO1")
    assert posts(calls, "/oauth/token")[0][2]["data"]["user_type"] == "Company"


def test_remints_only_when_the_agency_scope_changed_since_minting(agency, ghl_http):
    calls, _ = ghl_http
    oauth.get_location_access_token("LOC1")
    oauth.get_location_access_token("LOC1")
    assert len(posts(calls, "location-token")) == 1  # the location's own (narrower) scope does NOT trigger re-minting
    GhlOAuthToken.objects.filter(pk=agency.pk).update(scope="invoices.readonly oauth.write payments/transactions.readonly")
    oauth.get_location_access_token("LOC1")
    assert len(posts(calls, "location-token")) == 2
    oauth.get_location_access_token("LOC1")
    assert len(posts(calls, "location-token")) == 2


def test_uninstalled_location_does_not_get_a_token(agency, ghl_http):
    oauth.mint_location_token("LOC1")
    GhlOAuthToken.objects.filter(location_id="LOC1").update(uninstalled_at=timezone.now())
    with pytest.raises(oauth.GhlError):
        oauth.get_location_access_token("LOC1")
    assert oauth.has_usable_location_token("LOC1") is False


def test_without_an_agency_install_nothing_can_be_minted(ghl_http):
    with pytest.raises(oauth.GhlError):
        oauth.get_location_access_token("LOC1")


def test_install_webhook_gives_the_new_location_its_token_and_name(agency, ghl_http):
    r = APIClient().post("/api/v1/workspace/webhook/", {"type": "INSTALL", "locationId": "NEW1", "companyId": "CO1"}, format="json")
    assert r.status_code == 200
    assert GhlLocation.objects.get(location_id="NEW1").name == "Fetched Name"
    assert GhlOAuthToken.objects.get(location_id="NEW1").user_type == "Location"


# ----------------------------------------------------------- uninstall safety
def test_uninstall_marks_a_token_only_when_ghl_rejects_the_refresh_token(agency, ghl_http, monkeypatch):
    oauth.mint_location_token("LOC1")
    c = APIClient()

    def failing(status):
        def post(url, **kw):
            return Resp(status, {"error": "x"})

        return post

    # network failure and 5xx prove nothing: the app stays installed
    def boom(url, **kw):
        raise oauth.httpx.ConnectError("down")

    monkeypatch.setattr(oauth.httpx, "post", boom)
    c.post("/api/v1/workspace/webhook/", {"type": "UNINSTALL", "locationId": "LOC1"}, format="json")
    monkeypatch.setattr(oauth.httpx, "post", failing(503))
    c.post("/api/v1/workspace/webhook/", {"type": "UNINSTALL", "locationId": "LOC1"}, format="json")
    assert GhlOAuthToken.objects.get(location_id="LOC1").uninstalled_at is None
    # a forged event while the token still works: ignored, and the rotated refresh token is kept
    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda url, **kw: Resp(200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 3600, "userType": "Location"}),
    )
    c.post("/api/v1/workspace/webhook/", {"type": "UNINSTALL", "locationId": "LOC1"}, format="json")
    row = GhlOAuthToken.objects.get(location_id="LOC1")
    assert row.uninstalled_at is None and decrypt_secret(row.refresh_token_encrypted) == "R2"
    # GoHighLevel itself rejects the refresh token: now it is recorded
    monkeypatch.setattr(oauth.httpx, "post", failing(400))
    c.post("/api/v1/workspace/webhook/", {"type": "UNINSTALL", "locationId": "LOC1"}, format="json")
    assert GhlOAuthToken.objects.get(location_id="LOC1").uninstalled_at is not None


def test_company_uninstall_cascades_to_location_tokens_once_confirmed(agency, ghl_http, monkeypatch):
    oauth.mint_location_token("LOC1")
    oauth.mint_location_token("LOC2")
    monkeypatch.setattr(oauth.httpx, "post", lambda url, **kw: Resp(401, {"error": "invalid_grant"}))
    APIClient().post("/api/v1/workspace/webhook/", {"type": "UNINSTALL", "companyId": "CO1"}, format="json")
    assert GhlOAuthToken.objects.filter(uninstalled_at__isnull=False).count() == 3  # agency + both locations


def test_scheduled_refresh_renews_agency_and_location_tokens(agency, ghl_http):
    oauth.mint_location_token("LOC1")
    GhlOAuthToken.objects.update(expires_at=timezone.now() + timedelta(hours=1))
    assert oauth.refresh_expiring_tokens() == {"ok": 2, "failed": 0}
    assert all(decrypt_secret(t.refresh_token_encrypted) == "NEW-RT" for t in GhlOAuthToken.objects.all())
    assert oauth.refresh_expiring_tokens() == {"ok": 0, "failed": 0}  # nothing left that expires soon


# -------------------------------------------------------------------- admin
def test_admin_shows_which_locations_have_their_own_token_and_can_mint_them(agency, ghl_http):
    from django.contrib.auth import get_user_model
    from django.test import Client

    owner = get_user_model().objects.create_superuser("owner@example.com", "Adm1n-pass!")
    c = Client()
    c.force_login(owner)
    GhlLocation.objects.create(location_id="LOC1", name="Has token")
    GhlLocation.objects.create(location_id="LOC2", name="No token yet")
    oauth.mint_location_token("LOC1")
    page = c.get("/admin/ghl/ghllocation/").content.decode()
    assert "Has token" in page and "No token yet" in page and "Has own token" in page
    from django.contrib import admin
    from django.test import RequestFactory

    qs = admin.site._registry[GhlLocation].get_queryset(RequestFactory().get("/"))
    assert {loc.location_id: loc._has_token for loc in qs} == {"LOC1": True, "LOC2": False}
    assert qs.get(location_id="LOC1")._token_expires is not None
    ids = [str(x.pk) for x in GhlLocation.objects.filter(location_id="LOC2")]
    r = c.post("/admin/ghl/ghllocation/", {"action": "mint_tokens", "_selected_action": ids}, follow=True)
    assert "1 of 1 location(s) have their own token" in r.content.decode()
    assert GhlOAuthToken.objects.get(location_id="LOC2").user_type == "Location"
    tokens_page = c.get("/admin/ghl/ghloauthtoken/").content.decode()
    assert "Agency (Company token)" in tokens_page and "Has token" in tokens_page
