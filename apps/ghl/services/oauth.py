"""GoHighLevel OAuth token lifecycle.

Flow (see https://marketplace.gohighlevel.com/docs/ghl/oauth):
  1. An agency installs the app -> authorization code -> *Company* token (stored, encrypted).
  2. For every subaccount the app is installed on, the Company token is exchanged for that location's own
     *Location* token (POST /oauth/location-token) which is stored separately, with its own refresh token.
  3. Every call about a location's data uses that location's token. The Company token is only used to mint location
     tokens and to list installed locations.

Tokens are AES-256-GCM encrypted at rest. Refresh tokens rotate, so each refresh stores the new one, under a row lock so two
concurrent requests can never both spend the same refresh token.
"""

import logging
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.crypto import decrypt_secret, encrypt_secret

from ..models import GhlOAuthToken

logger = logging.getLogger(__name__)

API = "https://services.leadconnectorhq.com"
TOKEN_URL = f"{API}/oauth/token"
REFRESH_SKEW = timedelta(minutes=5)

# (path, Version header). The v3 paths are current; the legacy ones are tried only when v3 answers 404.
LOCATION_TOKEN_ENDPOINTS = (("/oauth/location-token", "v3"), ("/oauth/locationToken", "2021-07-28"))


class GhlError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _token_request(params: dict) -> dict:
    res = httpx.post(
        TOKEN_URL,
        data={"client_id": settings.GHL_CLIENT_ID, "client_secret": settings.GHL_CLIENT_SECRET, **params},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if res.status_code >= 400:
        raise GhlError(f"Token request failed ({res.status_code})", res.status_code)
    return res.json()


def _save_tokens(t: dict, row: GhlOAuthToken | None = None, minted_from_scope: str | None = None) -> GhlOAuthToken:
    fields = {
        "location_id": t.get("locationId"),
        "company_id": t.get("companyId"),
        "user_type": t.get("userType"),
        "access_token_encrypted": encrypt_secret(t["access_token"]),
        "refresh_token_encrypted": encrypt_secret(t["refresh_token"]),
        "scope": t.get("scope"),
        "expires_at": timezone.now() + timedelta(seconds=int(t["expires_in"])),
        "uninstalled_at": None,
    }
    if minted_from_scope is not None:
        fields["minted_from_scope"] = minted_from_scope
    if row:
        # A refresh response may omit identifiers: never blank out what we already know.
        for k, v in fields.items():
            if v is not None or k in ("uninstalled_at",):
                setattr(row, k, v)
        row.save()
        return row
    obj, _ = GhlOAuthToken.objects.update_or_create(company_id=fields["company_id"], location_id=fields["location_id"], defaults=fields)
    return obj


def exchange_code(code: str) -> dict:
    """Exchange the install code for tokens and store them encrypted."""
    t = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": settings.GHL_REDIRECT_URI})
    _save_tokens(t)
    return t


def _refresh_row(row: GhlOAuthToken) -> str:
    t = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": decrypt_secret(row.refresh_token_encrypted),
            "user_type": row.user_type or "Location",
        }
    )
    _save_tokens(t, row)  # refresh tokens rotate: always store the new one
    return t["access_token"]


def _valid_access_token(row_id) -> str:
    """Stored access token, refreshed if about to expire. The row is locked so concurrent callers refresh only once."""
    with transaction.atomic():
        row = GhlOAuthToken.objects.select_for_update().get(pk=row_id)
        if row.uninstalled_at:
            raise GhlError("App is not installed for this account")
        if row.expires_at - timezone.now() > REFRESH_SKEW:
            return decrypt_secret(row.access_token_encrypted)
        return _refresh_row(row)


def _agency_row() -> GhlOAuthToken | None:
    return (
        GhlOAuthToken.objects.filter(user_type="Company", location_id__isnull=True, uninstalled_at__isnull=True)
        .order_by("-updated_at")
        .first()
    )


def get_agency_access_token() -> tuple[str, str]:
    """(Company access token, company id). Used only to mint location tokens and list installed locations."""
    agency = _agency_row()
    if not agency or not agency.company_id:
        raise GhlError("App is not installed at the agency level")
    return _valid_access_token(agency.pk), agency.company_id


def get_location_access_token(location_id: str) -> str:
    """The location's OWN access token (minted from the agency token the first time), refreshed when needed."""
    row = GhlOAuthToken.objects.filter(location_id=location_id).first()
    if not row:
        return _mint(location_id)
    if row.uninstalled_at:
        raise GhlError("App is not installed for this subaccount")
    # The agency reinstalled with different permissions since this token was minted: mint again so it gets them too.
    agency = _agency_row()
    if agency and agency.scope and row.minted_from_scope is not None and row.minted_from_scope != agency.scope:
        return _mint(location_id)
    return _valid_access_token(row.pk)


def has_usable_location_token(location_id: str) -> bool:
    return GhlOAuthToken.objects.filter(location_id=location_id, uninstalled_at__isnull=True).exists()


def _request_location_token(agency_token: str, company_id: str, location_id: str) -> dict:
    last_status = None
    for path, version in LOCATION_TOKEN_ENDPOINTS:
        res = httpx.post(
            f"{API}{path}",
            data={"companyId": company_id, "locationId": location_id},
            headers={"Authorization": f"Bearer {agency_token}", "Version": version, "Accept": "application/json"},
            timeout=30,
        )
        if res.status_code == 404:  # this API generation is not served: try the next one
            last_status = 404
            continue
        if res.status_code >= 400:
            raise GhlError(f"Location token failed ({res.status_code})", res.status_code)
        return res.json()
    raise GhlError(f"Location token endpoint not found ({last_status})", last_status)


def _mint(location_id: str) -> str:
    """Exchange the agency token for this location's token and store it. Returns the new access token."""
    agency = _agency_row()
    if not agency or not agency.company_id:
        raise GhlError("App is not installed for this subaccount")
    agency_token = _valid_access_token(agency.pk)
    t = _request_location_token(agency_token, agency.company_id, location_id)
    payload = {
        **t,
        "locationId": t.get("locationId") or location_id,
        "companyId": agency.company_id,  # the response carries no companyId; we know it from the agency install
        "userType": "Location",
    }
    try:
        with transaction.atomic():
            _save_tokens(payload, minted_from_scope=agency.scope or "")
    except IntegrityError:  # another worker minted the same location at the same moment: theirs is just as good
        logger.info("Location %s was minted concurrently", location_id)
    return t["access_token"]


def mint_location_token(location_id: str) -> GhlOAuthToken:
    """Eagerly create (or renew) a location's own token from the agency token, e.g. at onboarding."""
    _mint(location_id)
    return GhlOAuthToken.objects.get(location_id=location_id)


def refresh_expiring_tokens(within: timedelta = timedelta(hours=6)) -> dict:
    """Scheduled job: renew every token (agency and locations) that expires within the window."""
    ok = failed = 0
    threshold = timezone.now() + within
    for row_id in list(GhlOAuthToken.objects.filter(uninstalled_at__isnull=True, expires_at__lt=threshold).values_list("pk", flat=True)):
        try:
            with transaction.atomic():
                row = GhlOAuthToken.objects.select_for_update().get(pk=row_id)
                if row.uninstalled_at or row.expires_at >= threshold:
                    continue  # someone refreshed it while we were iterating
                _refresh_row(row)
            ok += 1
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh GHL token %s", row_id)
            failed += 1
    return {"ok": ok, "failed": failed}


def _confirm_dead(row_id) -> bool:
    """True (and marked uninstalled) only when GoHighLevel itself rejects the stored refresh token (400/401).

    Network trouble or a 5xx proves nothing, so it never marks an app as uninstalled.
    """
    with transaction.atomic():
        row = GhlOAuthToken.objects.select_for_update().get(pk=row_id)
        try:
            _refresh_row(row)
            return False
        except GhlError as exc:
            if exc.status in (400, 401):
                row.uninstalled_at = timezone.now()
                row.save(update_fields=["uninstalled_at", "updated_at"])
                return True
            logger.warning("Uninstall check inconclusive for token %s: %s", row_id, exc)
        except httpx.HTTPError as exc:
            logger.warning("Uninstall check inconclusive for token %s: %s", row_id, exc)
    return False


def confirm_uninstall(location_id: str) -> None:
    """A location uninstall event is only believed after the stored refresh token is confirmed dead."""
    row = GhlOAuthToken.objects.filter(location_id=location_id).first()
    if row and not row.uninstalled_at:
        _confirm_dead(row.pk)


def confirm_company_uninstall(company_id: str) -> None:
    """The agency removed the app: confirm the agency token, then every location token that came from it."""
    agency = GhlOAuthToken.objects.filter(company_id=company_id, user_type="Company", location_id__isnull=True).first()
    if not agency or agency.uninstalled_at or not _confirm_dead(agency.pk):
        return
    for row_id in GhlOAuthToken.objects.filter(company_id=company_id, uninstalled_at__isnull=True, location_id__isnull=False).values_list(
        "pk", flat=True
    ):
        _confirm_dead(row_id)
