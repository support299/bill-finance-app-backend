import re

import pytest
from django.core import mail
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_register_login_refresh_profile():
    c = APIClient()
    r = c.post("/api/v1/auth/register/", {"email": "New@Example.com", "password": "Str0ng-pass!", "full_name": "New User"}, format="json")
    assert r.status_code == 201 and r.data["access"] and r.data["user"]["email"] == "new@example.com"

    r = c.post("/api/v1/auth/login/", {"email": "new@example.com", "password": "Str0ng-pass!"}, format="json")
    assert r.status_code == 200
    access, refresh = r.data["access"], r.data["refresh"]

    c.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert c.get("/api/v1/profile/").data["full_name"] == "New User"
    assert c.patch("/api/v1/profile/", {"full_name": "Renamed"}, format="json").data["full_name"] == "Renamed"

    c.credentials()
    assert c.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json").status_code == 200


def test_bad_credentials_and_unauthenticated():
    c = APIClient()
    assert c.post("/api/v1/auth/login/", {"email": "x@example.com", "password": "nope"}, format="json").status_code == 401
    assert c.get("/api/v1/ledgers/").status_code == 401
    assert c.get("/api/v1/transactions/").status_code == 401


def test_register_validates_and_rejects_duplicates(user_a):
    c = APIClient()
    r = c.post("/api/v1/auth/register/", {"email": "a@example.com", "password": "Str0ng-pass!"}, format="json")
    assert r.status_code == 400 and r.data["error"]["code"]
    assert c.post("/api/v1/auth/register/", {"email": "z@example.com", "password": "short"}, format="json").status_code == 400


def test_password_reset_flow(user_a):
    c = APIClient()
    assert c.post("/api/v1/auth/password-reset/", {"email": "nobody@example.com"}, format="json").status_code == 200
    assert len(mail.outbox) == 0  # unknown emails get the same response but no mail
    assert c.post("/api/v1/auth/password-reset/", {"email": "a@example.com"}, format="json").status_code == 200
    m = re.search(r"uid=([^&\s]+)&token=([^\s]+)", mail.outbox[0].body)
    r = c.post("/api/v1/auth/password-reset/confirm/", {"uid": m[1], "token": m[2], "password": "An0ther-pass!"}, format="json")
    assert r.status_code == 200
    assert c.post("/api/v1/auth/login/", {"email": "a@example.com", "password": "An0ther-pass!"}, format="json").status_code == 200
    # the link is single-use
    assert (
        c.post("/api/v1/auth/password-reset/confirm/", {"uid": m[1], "token": m[2], "password": "Third-pass-1!"}, format="json").status_code
        == 400
    )
