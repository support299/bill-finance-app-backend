"""Who is acting on a request: the GoHighLevel user behind an embedded session, when known.

Set by ActorJWTAuthentication from the token's ``ghl_actor`` claim, reset by ActorContextMiddleware at the start and end of
every request (a worker thread must never carry one request's actor into the next), and read by audit() so every change
records the person, not just the sub-account.
"""

import contextvars

_actor: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ghl_actor", default=None)


def set_actor(actor: dict | None) -> None:
    _actor.set(actor if isinstance(actor, dict) else None)


def current_actor() -> dict | None:
    return _actor.get()
