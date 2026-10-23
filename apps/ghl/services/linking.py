"""Linking a GoHighLevel location to an existing (email) workspace.

Proof of ownership is the GHL OAuth consent itself: after the install callback the server hands the browser a short-lived
signed code naming the location (and company). Only a signed-in user presenting that code can link it, and the UI asks them to
confirm. A bare location id is never enough.
"""

from django.core import signing
from rest_framework.exceptions import APIException, ValidationError

from apps.core.services import audit
from apps.ledger.models import Ledger, Transaction

from ..models import GhlLocation
from .oauth import has_usable_location_token

LINK_SALT = "ghl-link-confirm"
LINK_MAX_AGE = 15 * 60
TENANT_DOMAIN = "@tenants.invalid"


class Conflict(APIException):
    status_code = 409
    default_code = "conflict"


def make_link_code(location_id: str | None, company_id: str | None) -> str:
    return signing.dumps({"loc": location_id, "co": company_id}, salt=LINK_SALT)


def read_link_code(code: str) -> dict:
    try:
        return signing.loads(code, salt=LINK_SALT, max_age=LINK_MAX_AGE)
    except signing.SignatureExpired:
        raise ValidationError({"code": "This link has expired. Start the connection again."})
    except signing.BadSignature:
        raise ValidationError({"code": "This link is not valid."})


def is_tenant(user) -> bool:
    """Hidden per-location accounts created by the embedded sign-in (they have no password)."""
    return bool(user and user.email.endswith(TENANT_DOMAIN))


def _has_data(user) -> bool:
    return Ledger.objects.for_user(user).exists() or Transaction.objects.for_user(user).exists()


def link_state(loc: GhlLocation, user) -> str:
    """'free' | 'here' | 'elsewhere'."""
    if loc.user_id is None:
        return "free"
    if loc.user_id == user.id:
        return "here"
    if is_tenant(loc.user) and not _has_data(loc.user):
        return "free"  # an empty hidden tenant can be handed over safely
    return "elsewhere"


def summary(loc: GhlLocation, user=None) -> dict:
    out = {
        "location_id": loc.location_id,
        "name": loc.name,
        "company_id": loc.company_id,
        "email": loc.email,
        "active": loc.active,
        "has_token": has_usable_location_token(loc.location_id),
        "invoices_synced_at": loc.invoices_synced_at,
    }
    if user is not None:
        out["link_state"] = link_state(loc, user)
    return out


def link_location(loc: GhlLocation, user) -> GhlLocation:
    state = link_state(loc, user)
    if not loc.active:
        raise Conflict("This location has been turned off.")
    if state == "elsewhere":
        raise Conflict("This location is already linked to another workspace.")
    if state == "free":
        loc.user = user
        loc.invoices_synced_at = None  # force a first import into this workspace
        loc.save(update_fields=["user", "invoices_synced_at"])
        audit(user, "ghl_location_linked", "ghl_location", loc.location_id)
    return loc
