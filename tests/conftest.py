import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Tests must never reach Plaid/GoHighLevel: any un-mocked outbound HTTP request fails loudly."""
    import httpx

    def blocked(self, request, *a, **kw):
        raise RuntimeError(f"Real network call blocked in tests: {request.method} {request.url}")

    monkeypatch.setattr(httpx.Client, "send", blocked)


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.TOKEN_ENCRYPTION_KEY = "test-encryption-key"
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.SECURE_SSL_REDIRECT = False
    settings.PLAID_CLIENT_ID = "cid"
    settings.PLAID_SECRET = "secret"
    settings.CRON_SECRET = "cron-secret"
    settings.GHL_LINK_SECRET = ""
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_CLASSES": [],
    }


def make_user(email="a@example.com", **kw):
    return User.objects.create_user(email, "Str0ng-pass!", **kw)


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def user_a(db):
    return make_user("a@example.com")


@pytest.fixture
def user_b(db):
    return make_user("b@example.com")


@pytest.fixture
def api_a(user_a):
    return client_for(user_a)


@pytest.fixture
def api_b(user_b):
    return client_for(user_b)
