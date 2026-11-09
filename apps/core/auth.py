from rest_framework_simplejwt.authentication import JWTAuthentication

from .actor import set_actor


class ActorJWTAuthentication(JWTAuthentication):
    """Standard JWT auth that also remembers the GoHighLevel user named in the token (see actor.py)."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            _, token = result
            set_actor(token.get("ghl_actor") if hasattr(token, "get") else None)
        return result
