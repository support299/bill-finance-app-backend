import time

from rest_framework.permissions import SAFE_METHODS, BasePermission


class SupportReadOnly(BasePermission):
    """Sessions an agency admin opens on a client's workspace can look but never change anything, and expire."""

    message = "You are viewing this client in read-only mode. Changes are not allowed."

    def has_permission(self, request, view):
        token = getattr(request, "auth", None)
        if token is None or not hasattr(token, "get") or not token.get("support_readonly"):
            return True
        if time.time() > float(token.get("support_exp", 0)):
            self.message = "This support session has expired. Open the client again from the agency list."
            return False
        return request.method in SAFE_METHODS


# ------------------------------------------------------------------ GoHighLevel members (embedded SSO sessions)
ACCESS_LEVELS = ("none", "viewer", "editor", "admin")
_RANK = {level: i for i, level in enumerate(ACCESS_LEVELS)}


def member_access(request) -> str | None:
    """Effective access of the GoHighLevel user behind this request; None for sessions without one (full access).

    Resolved once per request from the token's ``ghl_actor`` claim and the member table, so an access change made by an
    administrator applies to the next request, not the next sign-in.
    """
    cached = getattr(request, "_member_access", False)
    if cached is not False:
        return cached
    token = getattr(request, "auth", None)
    actor = token.get("ghl_actor") if token is not None and hasattr(token, "get") else None
    access = None
    if isinstance(actor, dict) and actor.get("uid") and actor.get("loc"):
        from apps.ghl.services.sso import effective_access

        access = effective_access(actor["loc"], actor["uid"], actor.get("type"), actor.get("role"))
    request._member_access = access
    return access


class MemberAccess(BasePermission):
    """Viewers read, editors change, administrators manage people. Views may set ``required_access`` ("admin") or
    ``viewer_may_post`` (for harmless POSTs such as logout)."""

    message = "You do not have permission to do this."

    def has_permission(self, request, view):
        access = member_access(request)
        if access is None:
            return True
        if access == "none":
            self.message = "Your access to this sub-account's accounting has been turned off."
            return False
        required = getattr(view, "required_access", None)
        if required and _RANK[access] < _RANK[required]:
            self.message = "Only administrators can do this."
            return False
        if access == "viewer" and request.method not in SAFE_METHODS and not getattr(view, "viewer_may_post", False):
            self.message = "You have view-only access. Changes are not allowed."
            return False
        return True
