"""GoHighLevel subaccount (location) records: created at install time, with the location's name and details."""

import logging

import httpx
from django.conf import settings
from django.utils import timezone

from ..models import GhlLocation
from .invoices import ghl_get
from .oauth import API, GhlError, get_agency_access_token, has_usable_location_token, mint_location_token

logger = logging.getLogger(__name__)


def refresh_location_details(loc: GhlLocation) -> GhlLocation:
    """Fills name/email/phone/company from GoHighLevel. Failures are logged and leave the record as is."""
    try:
        payload = ghl_get(loc.location_id, f"/locations/{loc.location_id}")
    except Exception as exc:  # noqa: BLE001 - e.g. scope missing or token not minted yet
        logger.warning("Could not read details for location %s: %s", loc.location_id, exc)
        return loc
    d = payload.get("location") or payload
    if isinstance(d, dict):
        loc.name = d.get("name") or d.get("business", {}).get("name") or loc.name
        loc.email = d.get("email") or (d.get("business") or {}).get("email") or loc.email
        loc.phone = d.get("phone") or (d.get("business") or {}).get("phone") or loc.phone
        loc.company_id = d.get("companyId") or loc.company_id
        loc.details = d
        loc.details_synced_at = timezone.now()
        loc.save()
    return loc


def record_location(
    location_id: str,
    company_id: str | None = None,
    name: str | None = None,
    fetch_details: bool = True,
    require_verified: bool = False,
    mint: bool = True,
) -> GhlLocation:
    """Creates (or updates) the location row. Safe to call repeatedly; an existing user/active flag is never touched.

    require_verified: for untrusted callers (webhooks). A *new* row is only kept if GoHighLevel confirms the location
    by returning its details; otherwise the row is removed and GhlError is raised.
    """
    loc, created = GhlLocation.objects.get_or_create(location_id=location_id)
    if mint:
        ensure_location_token(location_id)  # the location's own token, minted from the agency token at onboarding
    changed = False
    if company_id and loc.company_id != company_id:
        loc.company_id, changed = company_id, True
    if name and loc.name != name:
        loc.name, changed = name, True
    if changed:
        loc.save()
    if fetch_details and (created or not loc.name):
        loc = refresh_location_details(loc)
    if require_verified and created and not loc.details_synced_at:
        loc.delete()
        raise GhlError("Location could not be verified with GoHighLevel")
    return loc


def list_installed_locations(agency_token: str, company_id: str):
    """Yields every subaccount the app is installed on. Uses the v3 API (pageToken); falls back to the legacy one on 404."""
    base = {"companyId": company_id, "appId": settings.GHL_APP_ID, "isInstalled": "true"}
    headers = {"Authorization": f"Bearer {agency_token}", "Accept": "application/json"}
    page_token = None
    while True:
        params = {**base, "pageSize": 100, **({"pageToken": page_token} if page_token else {})}
        res = httpx.get(f"{API}/oauth/installed-locations", params=params, headers={**headers, "Version": "v3"}, timeout=30)
        if res.status_code == 404:
            break  # v3 not served for this account: use the legacy endpoint below
        if res.status_code >= 400:
            raise GhlError(f"Installed locations request failed ({res.status_code})", res.status_code)
        body = res.json()
        yield from body.get("items") or []
        pagination = body.get("pagination") or {}
        page_token = pagination.get("nextPageToken")
        if not (pagination.get("hasNextPage") and page_token):
            return
    skip = 0
    while True:
        res = httpx.get(
            f"{API}/oauth/installedLocations",
            params={**base, "skip": skip, "limit": 100},
            headers={**headers, "Version": "2021-07-28"},
            timeout=30,
        )
        if res.status_code >= 400:
            raise GhlError(f"Installed locations request failed ({res.status_code})", res.status_code)
        rows = res.json().get("locations") or []
        yield from rows
        skip += len(rows)
        if len(rows) < 100:
            return


def ensure_location_token(location_id: str) -> bool:
    """Make sure the location has its own stored token, minting it from the agency token if not. True if one exists after."""
    if has_usable_location_token(location_id):
        return True
    try:
        mint_location_token(location_id)
        return True
    except Exception as exc:  # noqa: BLE001 - e.g. app not installed on that location; surfaced in the admin, retried by the daily sync
        logger.warning("Could not mint a token for location %s: %s", location_id, exc)
        return False


def sync_installed_locations() -> dict:
    """Records every subaccount the agency installed the app on (with its name) and gives each its own token."""
    from ..tasks import mint_location_token_task

    token, company_id = get_agency_access_token()
    seen = 0
    for row in list_installed_locations(token, company_id):
        lid = row.get("_id") or row.get("id")
        if not lid:
            continue
        record_location(lid, company_id=company_id, name=row.get("name"), fetch_details=False, mint=False)
        seen += 1
        if not has_usable_location_token(lid):
            mint_location_token_task.delay(lid)  # one task per location so a slow/failed one never blocks the rest
    return {"locations": seen}
