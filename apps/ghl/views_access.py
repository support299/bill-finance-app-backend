"""Who may open a sub-account's accounting, and what they did: SSO sign-in, members, sub-account switcher, activity."""

import logging

from django.conf import settings
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.actor import current_actor, set_actor
from apps.core.models import AuditLog
from apps.core.permissions import ACCESS_LEVELS, member_access
from apps.core.services import audit

from .models import GhlLocation, GhlMember
from .services import invoices as inv_svc
from .services import linking
from .services import sso as sso_svc
from .services import workspace
from .views import LOCATION_ID_RE

logger = logging.getLogger(__name__)


def _error(code: str, message: str, status: int = 403) -> Response:
    return Response({"error": {"code": code, "message": message, "details": None}}, status=status)


def _me(user, actor: dict | None, access: str | None, loc: GhlLocation | None, support: bool = False) -> dict:
    """What the page needs to shape itself: who you are, what you may do, where you are."""
    if support:
        level = "viewer"
    else:
        level = access or "owner"
    return {
        "actor": (
            {"id": actor["uid"], "name": actor.get("name"), "email": actor.get("email"), "type": actor.get("type"), "role": actor.get("role")}
            if actor
            else None
        ),
        "access": level,
        "can_edit": level in ("editor", "admin", "owner"),
        "can_manage_users": not support and (access == "admin" or (access is None and loc is not None)),
        "can_switch": bool(actor and actor.get("type") == "agency"),
        "location": {"location_id": loc.location_id, "name": loc.name} if loc else None,
    }


class SsoSessionView(APIView):
    """Embedded sign-in: the page forwards the encrypted user data GoHighLevel handed it; ``id`` optionally names another
    sub-account of the same agency (the switcher)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "auth"

    def post(self, request):
        if not settings.GHL_SSO_SECRET:
            return _error("not_configured", "GoHighLevel sign-in is not configured on the server.", 503)
        try:
            sso = sso_svc.parse_user(sso_svc.decrypt_user_data(str(request.data.get("payload", "")), settings.GHL_SSO_SECRET))
        except ValueError:
            return _error("sso_invalid", "Could not verify your GoHighLevel sign-in. Reload the page inside GoHighLevel.")

        location_id = str(request.data.get("id") or sso.active_location or "")
        if not LOCATION_ID_RE.match(location_id):
            return _error("forbidden", "This link is not valid.")
        if location_id != sso.active_location:
            # Switching sub-accounts: only agency staff, only within their own agency.
            target = GhlLocation.objects.filter(location_id=location_id).first()
            if sso.type != "agency" or not target or target.company_id != sso.company_id:
                return _error("forbidden", "That sub-account is not part of your agency.")

        try:
            loc, user = workspace.open_location(location_id)
        except workspace.WorkspaceDenied as exc:
            return _error(exc.code, str(exc))
        if not loc.company_id:
            loc.company_id = sso.company_id
            loc.save(update_fields=["company_id"])

        sso_svc.record_member(loc, sso)
        actor = sso.actor(location_id)
        set_actor(actor)  # so this sign-in (and the denial below) is logged against the person
        access = sso_svc.effective_access(location_id, sso.user_id, sso.type, sso.role)
        if access == "none":
            audit(user, "ghl_sso_denied", "ghl_location", location_id)
            return _error(
                "no_access",
                "You don't have access to accounting for this sub-account. Ask your agency administrator to turn it on.",
            )
        audit(user, "ghl_sso_sign_in", "ghl_location", location_id, {"access": access})

        refresh = RefreshToken.for_user(user)
        refresh["ghl_actor"] = actor
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user_id": str(user.id),
                "me": _me(user, actor, access, loc),
            }
        )


def _session_location(request, actor: dict | None) -> GhlLocation | None:
    if actor:
        return GhlLocation.objects.filter(location_id=actor["loc"], user=request.user).first()
    return GhlLocation.objects.filter(user=request.user).order_by("name").first()


class MeView(APIView):
    def get(self, request):
        actor = current_actor()
        token = getattr(request, "auth", None)
        support = bool(token is not None and hasattr(token, "get") and token.get("support_readonly"))
        return Response(_me(request.user, actor, member_access(request), _session_location(request, actor), support))


# ----------------------------------------------------------------------------------------- members
def _ghl_users(loc: GhlLocation) -> tuple[list[dict], str | None]:
    """Everyone GoHighLevel lists for the sub-account (needs the users.readonly scope); (rows, error message)."""
    try:
        payload = inv_svc.ghl_get(loc.location_id, f"/users/?locationId={loc.location_id}")
    except Exception as exc:  # noqa: BLE001 - scope missing, token gone: the list still works from sign-ins alone
        logger.info("Could not list GoHighLevel users for %s: %s", loc.location_id, exc)
        return [], "GoHighLevel did not return the user list (the app may need the users.readonly permission)."
    out = []
    for u in payload.get("users", []) if isinstance(payload, dict) else []:
        if not isinstance(u, dict) or not u.get("id"):
            continue
        roles = u.get("roles") or {}
        type_ = str(roles.get("type") or "location").lower()
        out.append(
            {
                "ghl_user_id": str(u["id"]),
                "name": u.get("name") or " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x) or None,
                "email": (u.get("email") or "").lower() or None,
                "ghl_type": "agency" if type_ == "agency" else "location",
                "ghl_role": str(roles.get("role") or "user").lower(),
            }
        )
    return out, None


def _member_row(loc: GhlLocation, m: GhlMember | None, ghl: dict | None, you: str | None) -> dict:
    uid = m.ghl_user_id if m else ghl["ghl_user_id"]
    ghl_type = (ghl or {}).get("ghl_type") or (m.ghl_type if m else None)
    ghl_role = (ghl or {}).get("ghl_role") or (m.ghl_role if m else None)
    default = sso_svc.default_access(ghl_type, ghl_role)
    return {
        "location_id": loc.location_id,
        "ghl_user_id": uid,
        "name": (ghl or {}).get("name") or (m.name if m else None),
        "email": (ghl or {}).get("email") or (m.email if m else None),
        "ghl_type": ghl_type,
        "ghl_role": ghl_role,
        "default_access": default,
        "access": m.access if m else None,
        "effective_access": (m.access if m and m.access else default),
        "last_seen_at": m.last_seen_at if m else None,
        "access_set_by": m.access_set_by if m else None,
        "access_set_at": m.access_set_at if m else None,
        "is_you": uid == you,
    }


class MembersView(APIView):
    """Administrators: everyone who can (or cannot) open this sub-account's accounting."""

    required_access = "admin"

    def get(self, request):
        actor = current_actor()
        locs = GhlLocation.objects.filter(user=request.user).order_by("name")
        if actor:
            locs = locs.filter(location_id=actor["loc"])
        rows, warnings = [], []
        for loc in locs:
            members = {m.ghl_user_id: m for m in loc.members.all()}
            ghl_users, err = _ghl_users(loc)
            if err:
                warnings.append(err)
            seen = set()
            for g in ghl_users:
                seen.add(g["ghl_user_id"])
                rows.append(_member_row(loc, members.get(g["ghl_user_id"]), g, actor and actor["uid"]))
            for uid, m in members.items():
                if uid not in seen:
                    rows.append(_member_row(loc, m, None, actor and actor["uid"]))
        rows.sort(key=lambda r: ((r["name"] or r["email"] or "").lower(), r["ghl_user_id"]))
        return Response({"members": rows, "warnings": warnings, "levels": list(ACCESS_LEVELS)})


class MemberDetailView(APIView):
    required_access = "admin"

    def put(self, request, location_id, ghl_user_id):
        loc = GhlLocation.objects.filter(location_id=location_id, user=request.user).first()
        if not loc:
            return _error("not_found", "Sub-account not found.", 404)
        actor = current_actor()
        if actor and actor["loc"] != location_id:
            return _error("forbidden", "That sub-account is not the one you are signed in to.")
        if actor and actor["uid"] == ghl_user_id:
            raise ValidationError({"access": "You cannot change your own access."})
        access = request.data.get("access")
        if access is not None and access not in ACCESS_LEVELS:
            raise ValidationError({"access": f"Choose one of {', '.join(ACCESS_LEVELS)}, or null for automatic."})
        if not str(ghl_user_id).strip() or len(ghl_user_id) > 64:
            raise ValidationError({"ghl_user_id": "Invalid user id."})

        m, _ = GhlMember.objects.get_or_create(location=loc, ghl_user_id=ghl_user_id)
        for field in ("name", "email", "ghl_type", "ghl_role"):  # details for a user who has not signed in yet
            if request.data.get(field) and not getattr(m, field):
                setattr(m, field, str(request.data[field])[:200])
        m.access = access
        m.access_set_by = (actor or {}).get("name") or (actor or {}).get("email") or request.user.email
        m.access_set_at = timezone.now()
        m.save()
        audit(
            request.user,
            "member_access_changed",
            "ghl_member",
            ghl_user_id,
            {"name": m.name, "email": m.email, "access": access or "automatic", "location_id": location_id},
        )
        return Response(_member_row(loc, m, None, actor and actor["uid"]))


# ---------------------------------------------------------------------------------------- switcher
class SwitchLocationsView(APIView):
    """Agency staff: the sub-accounts of their agency they can move between."""

    def get(self, request):
        actor = current_actor()
        if not actor or actor.get("type") != "agency" or not actor.get("co"):
            return _error("forbidden", "Only agency users can switch sub-accounts.")
        locs = (
            GhlLocation.objects.filter(company_id=actor["co"], active=True)
            .select_related("user")
            .order_by(Lower("name").asc(nulls_last=True), "location_id")
        )
        return Response(
            {
                "locations": [
                    {
                        "location_id": loc.location_id,
                        "name": loc.name,
                        "current": loc.location_id == actor["loc"],
                        "can_open": not loc.user_id or linking.is_tenant(loc.user),
                    }
                    for loc in locs
                ]
            }
        )


# ---------------------------------------------------------------------------------------- activity
class ActivityView(APIView):
    """Administrators: who changed what in this workspace. Filters: actor, from, to (dates), q (action contains)."""

    required_access = "admin"
    MAX_PAGE_SIZE = 100

    def get(self, request):
        p = request.query_params
        qs = AuditLog.objects.filter(user=request.user)
        if p.get("actor"):
            qs = qs.filter(actor_id=p["actor"]) if p["actor"] != "system" else qs.filter(actor_id__isnull=True)
        if p.get("from") and (d := parse_date(p["from"])):
            qs = qs.filter(created_at__date__gte=d)
        if p.get("to") and (d := parse_date(p["to"])):
            qs = qs.filter(created_at__date__lte=d)
        if term := (p.get("q") or "").strip():
            qs = qs.filter(Q(action__icontains=term) | Q(entity_id__icontains=term) | Q(actor_name__icontains=term))
        try:
            page_size = min(max(int(p.get("page_size", 50)), 1), self.MAX_PAGE_SIZE)
            page_no = max(int(p.get("page", 1)), 1)
        except ValueError:
            raise ValidationError({"page": "Page and page_size must be numbers."})
        paginator = Paginator(qs, page_size)
        try:
            page = paginator.page(page_no)
        except EmptyPage:
            page = paginator.page(paginator.num_pages)
        actors = (
            AuditLog.objects.filter(user=request.user, actor_id__isnull=False)
            .values("actor_id", "actor_name", "actor_email")
            .distinct()
            .order_by("actor_name")
        )
        return Response(
            {
                "rows": [
                    {
                        "id": str(a.id),
                        "at": a.created_at,
                        "action": a.action,
                        "entity": a.entity,
                        "entity_id": a.entity_id,
                        "metadata": a.metadata,
                        "actor": {"id": a.actor_id, "name": a.actor_name, "email": a.actor_email} if a.actor_id else None,
                    }
                    for a in page.object_list
                ],
                "count": paginator.count,
                "page": page.number,
                "total_pages": paginator.num_pages,
                "actors": [{"id": x["actor_id"], "name": x["actor_name"], "email": x["actor_email"]} for x in actors],
            }
        )
