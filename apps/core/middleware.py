from .actor import set_actor


class ActorContextMiddleware:
    """Clears the per-request actor before and after each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_actor(None)
        try:
            return self.get_response(request)
        finally:
            set_actor(None)
