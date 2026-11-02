"""Agency support access: opening ``/agency?company=<id>&token=<GHL_AGENCY_TOKEN>`` lists that agency's onboarded clients.

Anyone holding the link can open the list, so the token is a secret. Clients are then viewed read-only and every access is audited.
"""

import hmac
import time
from datetime import timedelta

from django.conf import settings
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User

from ..models import GhlAgency, GhlLocation, GhlOAuthToken

AGENCY_DOMAIN = "@agencies.invalid"


def company_is_known(company_id: str) -> bool:
    return (
        GhlOAuthToken.objects.filter(company_id=company_id, uninstalled_at__isnull=True).exists()
        or GhlLocation.objects.filter(company_id=company_id).exists()
    )


def verify_access(company_id: str, token: str) -> str:
    """The link carries the company id and the shared token from GHL_AGENCY_TOKEN; both must check out."""
    expected = settings.GHL_AGENCY_TOKEN
    if not expected:
        raise PermissionDenied("Agency access is not configured on the server.")
    if not company_id or not hmac.compare_digest(token.encode(), expected.encode()):
        raise PermissionDenied("This agency link is not valid.")
    if not company_is_known(company_id):
        raise PermissionDenied("This agency has not installed the app.")
    return company_id


def agency_user(company_id: str) -> GhlAgency:
    agency = GhlAgency.objects.select_related("user").filter(company_id=company_id).first()
    if agency:
        return agency
    user = User.objects.create_user(f"agency-{company_id.lower()}{AGENCY_DOMAIN}", None, full_name=f"Agency {company_id}")
    return GhlAgency.objects.create(company_id=company_id, user=user)


def is_agency_user(user) -> bool:
    return bool(user and user.email.endswith(AGENCY_DOMAIN))


def agency_of(user) -> GhlAgency:
    agency = GhlAgency.objects.filter(user=user).first() if is_agency_user(user) else None
    if not agency:
        raise PermissionDenied("Agency access required.")
    return agency


def issue_support_tokens(tenant_user, agency_user_obj, location_id: str) -> dict:
    """Tokens for a client's workspace that can only read, expire, and say who opened them."""
    refresh = RefreshToken.for_user(tenant_user)
    refresh["support_readonly"] = True
    refresh["support_exp"] = int(time.time() + timedelta(hours=settings.SUPPORT_SESSION_HOURS).total_seconds())
    refresh["support_by"] = str(agency_user_obj.id)
    refresh["support_location"] = location_id
    return {"access": str(refresh.access_token), "refresh": str(refresh)}
