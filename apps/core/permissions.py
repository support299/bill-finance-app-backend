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
