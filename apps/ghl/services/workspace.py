"""Opening a sub-account's workspace: the location row and the hidden tenant user that owns its data."""

import logging

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.core.services import audit

from ..models import GhlLocation
from . import invoices as inv_svc
from . import linking, oauth
from . import locations as loc_svc

logger = logging.getLogger(__name__)
User = get_user_model()


class WorkspaceDenied(Exception):
    def __init__(self, message: str, code: str = "forbidden"):
        super().__init__(message)
        self.code = code


def open_location(location_id: str) -> tuple[GhlLocation, "User"]:
    """Finds or creates the location and its tenant user, refreshes details and imports invoices on the first visit."""
    loc = GhlLocation.objects.filter(location_id=location_id).first()
    if loc and not loc.active:
        raise WorkspaceDenied("This subaccount has been turned off.")

    user = loc.user if loc and loc.user_id else None
    if user and not linking.is_tenant(user):
        # Linked to a personal (email) workspace: a location id alone must never open it.
        raise WorkspaceDenied("This location is linked to a personal workspace. Sign in with your email to open it.")
    if not user:
        user = User.objects.create_user(
            f"ghl-{location_id.lower()}@tenants.invalid", None, full_name=f"GHL {location_id}"
        )  # unusable password: only reachable through the embedded sign-in
        if loc:
            loc.user = user
            loc.save(update_fields=["user"])
        else:
            loc = GhlLocation.objects.create(location_id=location_id, user=user)
        audit(user, "ghl_location_created", "ghl_location", location_id)

    loc.last_access_at = timezone.now()
    loc.save(update_fields=["last_access_at"])
    if not loc.name:  # first sign-in for a location we only knew by id
        loc_svc.refresh_location_details(loc)

    try:  # make sure this subaccount has its own stored token
        oauth.get_location_access_token(location_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Subaccount token unavailable: %s", exc)

    if not loc.invoices_synced_at:  # first visit: import existing invoices once
        try:
            inv_svc.sync_location_invoices(location_id, user)
        except Exception:  # noqa: BLE001
            logger.exception("Invoice import failed")
    return loc, user
