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

    @property
    def rejected(self) -> bool:
        """GoHighLevel itself refused (400/401), as opposed to a network problem or a 5xx."""
        return self.status in (400, 401)


def _error_detail(res) -> str:
    """The "message" GoHighLevel puts in an error body (e.g. "Location is not active"), for logs and errors."""
    try:
        body = res.json()
    except Exception:  # noqa: BLE001 - not JSON
        return ""
    msg = body.get("message") if isinstance(body, dict) else None
    if isinstance(msg, list):
        msg = "; ".join(str(m) for m in msg)
    return str(msg or body.get("error") or "")[:200] if isinstance(body, dict) else ""


def _token_request(params: dict) -> dict:
    res = httpx.post(
        TOKEN_URL,
        data={"client_id": settings.GHL_CLIENT_ID, "client_secret": settings.GHL_CLIENT_SECRET, **params},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if res.status_code >= 400:
        raise GhlError(f"Token request failed ({res.status_code}): {_error_detail(res)}", res.status_code)
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
    try:
        return _valid_access_token(row.pk)
    except GhlError as exc:
        if not exc.rejected:
            raise
        # The stored refresh token is dead (e.g. rotated by a refresh that was interrupted before it was saved).
        # The agency token can mint a new one for this location, unless GoHighLevel has deactivated the location.
        logger.info("Refresh of location %s token rejected (%s): minting a new one", location_id, exc)
        return _mint(location_id)


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
            raise GhlError(f"Location token failed ({res.status_code}): {_error_detail(res)}", res.status_code)
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


def _retire_location_token(row: GhlOAuthToken, reason: str) -> None:
    """Stop refreshing a location token GoHighLevel will not renew or replace (e.g. the location was deactivated).

    Only the token is retired: the GhlLocation row and its `active` flag (an admin decision) are left alone. The daily
    sync_installed_locations sees the location has no usable token and mints a fresh one as soon as GoHighLevel allows it.
    """
    row.uninstalled_at = timezone.now()
    row.save(update_fields=["uninstalled_at", "updated_at"])
    logger.warning("Retired token of location %s until the daily sync can mint a new one: %s", row.location_id, reason)


def refresh_expiring_tokens(within: timedelta = timedelta(hours=6)) -> dict:
    """Scheduled job: renew every token (agency and locations) that expires within the window.

    A location whose refresh token GoHighLevel rejects gets a new token minted from the agency token. When that is
    refused too (typically "Location is not active"), its token is retired so it is not retried every hour; the daily
    sync mints again once the location is back.
    """
    ok = failed = retired = 0
    threshold = timezone.now() + within
    for row_id in list(GhlOAuthToken.objects.filter(uninstalled_at__isnull=True, expires_at__lt=threshold).values_list("pk", flat=True)):
        try:
            with transaction.atomic():
                row = GhlOAuthToken.objects.select_for_update().get(pk=row_id)
                if row.uninstalled_at or row.expires_at >= threshold:
                    continue  # someone refreshed it while we were iterating
                try:
                    _refresh_row(row)
                except GhlError as exc:
                    if not (exc.rejected and row.location_id):
                        raise
                    logger.info("Refresh of location %s token rejected (%s): minting a new one", row.location_id, exc)
                    try:
                        _mint(row.location_id)
                    except GhlError as mint_exc:
                        if not mint_exc.rejected:
                            raise
                        _retire_location_token(row, str(mint_exc))
                        retired += 1
                        continue
            ok += 1
        except Exception:  # noqa: BLE001
            logger.exception("Could not refresh GHL token %s", row_id)
            failed += 1
    return {"ok": ok, "failed": failed, "retired": retired}


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
