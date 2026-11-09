"""Minimal server-side Plaid REST client. Credentials come from settings and never reach the browser."""

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class PlaidError(Exception):
    def __init__(self, message: str, error_code: str | None = None, error_type: str | None = None, status: int = 500):
        super().__init__(message)
        self.error_code = error_code
        self.error_type = error_type
        self.status = status


def plaid_env() -> str:
    return "production" if settings.PLAID_ENV.lower() == "production" else "sandbox"


def _base_url() -> str:
    return "https://production.plaid.com" if plaid_env() == "production" else "https://sandbox.plaid.com"


def is_configured() -> bool:
    return bool(settings.PLAID_CLIENT_ID and settings.PLAID_SECRET)


def plaid_request(path: str, body: dict) -> dict:
    if not is_configured():
        raise PlaidError("Plaid is not configured on the server.", "MISSING_CONFIG", "CONFIG", 500)
    try:
        res = httpx.post(
            f"{_base_url()}{path}",
            json={**body, "client_id": settings.PLAID_CLIENT_ID, "secret": settings.PLAID_SECRET},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise PlaidError("Could not reach the bank data provider.", "NETWORK_ERROR", "NETWORK", 503) from exc
    try:
        data = res.json() if res.content else {}
    except ValueError:
        data = {}
    if res.status_code >= 400:
        # Never surface raw provider payloads (may echo request metadata).
        raise PlaidError(
            data.get("error_message") if isinstance(data.get("error_message"), str) else "Bank provider request failed.",
            data.get("error_code") if isinstance(data.get("error_code"), str) else None,
            data.get("error_type") if isinstance(data.get("error_type"), str) else None,
            res.status_code,
        )
    return data


def friendly_plaid_error(code: str | None) -> str:
    return {
        "ITEM_LOGIN_REQUIRED": "Your bank needs you to sign in again. Use Reconnect to restore the connection.",
        "PENDING_EXPIRATION": "This bank connection will expire soon. Please reconnect it.",
        "PENDING_DISCONNECT": "This bank connection will expire soon. Please reconnect it.",
        "INSTITUTION_DOWN": "Your bank is temporarily unavailable. We'll retry automatically.",
        "INSTITUTION_NOT_RESPONDING": "Your bank is temporarily unavailable. We'll retry automatically.",
        "RATE_LIMIT_EXCEEDED": "Too many requests right now. Please try again in a few minutes.",
        "INVALID_ACCESS_TOKEN": "The saved bank credentials are no longer valid. Please reconnect.",
        "INVALID_CREDENTIALS": "The saved bank credentials are no longer valid. Please reconnect.",
        "MISSING_CONFIG": "Bank connectivity isn't configured yet. Add your provider credentials in settings.",
        "NETWORK_ERROR": "Network problem contacting your bank. Please try again.",
    }.get(code or "", "We couldn't complete the bank request. Please try again.")


def create_link_token(user_id: str, webhook: str | None = None, access_token: str | None = None, days_requested: int = 730) -> dict:
    body: dict = {
        "user": {"client_user_id": user_id},
        "client_name": "ImaxAccounting",
        "country_codes": ["US"],
        "language": "en",
    }
    if access_token:
        body["access_token"] = access_token  # update mode: reconnect an existing item
    else:
        body["products"] = ["transactions"]
        body["transactions"] = {"days_requested": max(1, min(int(days_requested), 730))}
    if webhook:
        body["webhook"] = webhook
    return plaid_request("/link/token/create", body)


def exchange_public_token(public_token: str) -> dict:
    return plaid_request("/item/public_token/exchange", {"public_token": public_token})


def transactions_sync(access_token: str, cursor: str | None) -> dict:
    body: dict = {"access_token": access_token, "count": 500}
    if cursor:
        body["cursor"] = cursor
    return plaid_request("/transactions/sync", body)


def transactions_refresh(access_token: str) -> dict:
    return plaid_request("/transactions/refresh", {"access_token": access_token})


def accounts_get(access_token: str) -> dict:
    return plaid_request("/accounts/get", {"access_token": access_token})


def item_get(access_token: str) -> dict:
    return plaid_request("/item/get", {"access_token": access_token})


def item_remove(access_token: str) -> dict:
    return plaid_request("/item/remove", {"access_token": access_token})


def webhook_verification_key(key_id: str) -> dict:
    return plaid_request("/webhook_verification_key/get", {"key_id": key_id})
