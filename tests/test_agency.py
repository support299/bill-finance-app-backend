import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.ghl.models import GhlLocation, GhlOAuthToken
from apps.ledger.models import Transaction

pytestmark = pytest.mark.django_db
TOKEN = "agency-token-123"
COMPANY = "COMP1"


@pytest.fixture
def agency_setup(settings):
    settings.GHL_AGENCY_TOKEN = TOKEN
    GhlOAuthToken.objects.create(
        company_id=COMPANY, access_token_encrypted="x", refresh_token_encrypted="y", expires_at="2099-01-01T00:00:00Z"
    )
    tenant = User.objects.create_user("ghl-loc1@tenants.invalid", None)
    other_tenant = User.objects.create_user("ghl-loc2@tenants.invalid", None)
    GhlLocation.objects.create(location_id="loc1", user=tenant, name="Smith Plumbing", company_id=COMPANY)
    GhlLocation.objects.create(location_id="loc2", user=other_tenant, name="Other agency client", company_id="COMP2")
    Transaction.objects.create(user=tenant, amount=-5, transaction_date="2026-03-01", merchant_name="x")
    return tenant


def login(company=COMPANY, token=TOKEN):
    c = APIClient()
    r = c.post("/api/v1/agency/session/", {"company_id": company, "token": token}, format="json")
    return c, r


def test_agency_lists_only_their_clients_and_opens_read_only(agency_setup):
    tenant = agency_setup
    c, r = login()
    assert r.status_code == 200, r.data
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    clients = c.get("/api/v1/agency/clients/").data["clients"]
    assert [x["location_id"] for x in clients] == ["loc1"]  # the other agency's client is invisible
    assert clients[0]["transaction_count"] == 1 and clients[0]["can_open"]

    assert c.post("/api/v1/agency/clients/loc2/open/").status_code == 404  # not this agency's client
    opened = c.post("/api/v1/agency/clients/loc1/open/")
    assert opened.status_code == 200 and opened.data["client_name"] == "Smith Plumbing"
    view = APIClient()
    view.credentials(HTTP_AUTHORIZATION=f"Bearer {opened.data['access']}")
    assert view.get("/api/v1/transactions/").data["count"] == 1  # reads work
    assert view.post("/api/v1/transactions/recategorize/").status_code == 403  # writes do not
    assert view.post("/api/v1/plaid/sync/").status_code == 403
    assert Transaction.objects.filter(user=tenant).count() == 1
    assert AuditLog.objects.filter(action="agency_open_client", entity_id="loc1").exists()
    assert AuditLog.objects.filter(action="agency_sign_in").exists()


def test_agency_login_needs_matching_token_and_a_known_company(agency_setup, settings):
    assert login(token="wrong")[1].status_code == 403
    assert login(token="")[1].status_code == 403
    assert login(company="")[1].status_code == 403
    assert login(company="UNKNOWN")[1].status_code == 403  # token fine, but that agency never installed the app
    assert login(company="COMP2")[1].status_code == 200  # known through its location
    settings.GHL_AGENCY_TOKEN = ""  # not configured = closed, even for an empty token
    assert login(token="")[1].status_code == 403


def test_normal_and_tenant_users_cannot_use_agency_endpoints(agency_setup, user_a):
    tenant = agency_setup
    from rest_framework_simplejwt.tokens import RefreshToken

    for u in (user_a, tenant):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(u).access_token}")
        assert c.get("/api/v1/agency/clients/").status_code == 403
        assert c.post("/api/v1/agency/clients/loc1/open/").status_code == 403


def test_support_session_expires(agency_setup, settings):
    tenant = agency_setup
    settings.SUPPORT_SESSION_HOURS = -1
    from apps.ghl.services.agency import issue_support_tokens

    tokens = issue_support_tokens(tenant, tenant, "loc1")
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    assert c.get("/api/v1/transactions/").status_code == 403


def test_clients_list_search_filter_and_pagination_cover_every_client(agency_setup):
    tenant = agency_setup  # loc1 "Smith Plumbing" (opened)
    for i in range(60):  # a big agency: 60 clients that never opened the app
        GhlLocation.objects.create(location_id=f"never{i:02d}", name=f"Client {i:02d}", company_id=COMPANY, email=f"c{i}@x.com")
    GhlLocation.objects.create(location_id="zz9", name="zeta needle", company_id=COMPANY)
    GhlLocation.objects.create(location_id="noname", company_id=COMPANY)  # unnamed clients sort last
    c, r = login()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")

    first = c.get("/api/v1/agency/clients/?page_size=25").data
    assert (first["count"], first["total_pages"], len(first["clients"])) == (63, 3, 25)
    assert first["counts"] == {"all": 63, "opened": 1, "not_opened": 62}
    names = [x["name"] for x in first["clients"]]
    assert names == sorted(names, key=str.lower)  # case-insensitive A-Z
    last = c.get("/api/v1/agency/clients/?page_size=25&page=3").data
    assert len(last["clients"]) == 13 and last["clients"][-1]["location_id"] == "noname"
    assert c.get("/api/v1/agency/clients/?page_size=25&page=99").data["page"] == 3  # out-of-range page falls back to the last

    # search reaches clients on pages the browser has not loaded, by name, email or id
    assert [x["location_id"] for x in c.get("/api/v1/agency/clients/?q=needle").data["clients"]] == ["zz9"]
    assert [x["location_id"] for x in c.get("/api/v1/agency/clients/?q=c59@x").data["clients"]] == ["never59"]
    assert c.get("/api/v1/agency/clients/?q=NEVER5").data["count"] == 10
    # status filter + counts follow the search
    opened = c.get("/api/v1/agency/clients/?status=opened").data
    assert [x["location_id"] for x in opened["clients"]] == ["loc1"] and opened["clients"][0]["transaction_count"] == 1
    nope = c.get("/api/v1/agency/clients/?status=not_opened&q=Client 0").data
    assert nope["count"] == 10 and nope["counts"]["opened"] == 0
    # never leaks another agency's clients, even when searched for by name
    assert c.get("/api/v1/agency/clients/?q=Other agency").data["count"] == 0
    assert c.get("/api/v1/agency/clients/?page=abc").status_code == 400
