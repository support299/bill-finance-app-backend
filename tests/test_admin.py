import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client

from apps.banking.models import BankAccount, PlaidItem
from apps.core.crypto import encrypt_secret
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff():
    user = get_user_model().objects.create_superuser("admin@example.com", "Adm1n-pass!")
    c = Client()
    c.force_login(user)
    return c


APP_LABELS = ("accounts", "core", "banking", "ledger", "reporting", "ghl")


def test_every_app_model_is_registered():
    registered = {m._meta.label_lower for m in admin.site._registry}
    from django.apps import apps

    missing = [
        m._meta.label_lower
        for label in APP_LABELS
        for m in apps.get_app_config(label).get_models()
        if m._meta.auto_created is False and m._meta.label_lower not in registered
    ]
    assert missing == []


def test_admin_index_lists_the_app_sections(staff):
    html = staff.get("/admin/").content.decode()
    for section in ("Accounts", "Banking", "Ledger", "Ghl", "Reporting", "Core"):
        assert f">{section}<" in html


def test_all_changelist_and_add_pages_load(staff):
    for model in admin.site._registry:
        if model._meta.app_label not in APP_LABELS:
            continue
        base = f"/admin/{model._meta.app_label}/{model._meta.model_name}/"
        assert staff.get(base).status_code == 200, base
        r = staff.get(base + "add/")
        assert r.status_code in (200, 403), base  # 403 for deliberately read-only models


def test_owner_sees_every_column_including_encrypted_and_decrypted_secrets(staff, user_a):
    item = PlaidItem.objects.create(
        user=user_a,
        item_id="it1",
        institution_name="Bank",
        access_token_encrypted=encrypt_secret("super-secret-token"),
        sync_cursor="cursor-123",
    )
    BankAccount.objects.create(user=user_a, plaid_item=item, plaid_account_id="a1", account_name="Checking")
    page = staff.get(f"/admin/banking/plaiditem/{item.pk}/change/")
    assert page.status_code == 200
    body = page.content.decode()
    assert item.access_token_encrypted in body  # ciphertext shown
    assert "super-secret-token" in body  # decrypted for the superuser
    assert "cursor-123" in body and str(item.pk) in body  # sync cursor and id are visible too
    assert "Checking" in body
    # every reveal is recorded
    log = AuditLog.objects.get(action="admin_reveal_secret")
    assert log.entity == "banking.plaiditem" and log.entity_id == str(item.pk) and log.metadata == {"field": "access_token_encrypted"}
    assert staff.get("/admin/banking/plaiditem/?q=Bank").status_code == 200


def test_vendor_tin_and_ghl_tokens_are_visible_to_owner(staff, user_a):
    from datetime import timedelta

    from django.utils import timezone

    from apps.ghl.models import GhlOAuthToken
    from apps.ledger.models import Ledger, VendorTaxProfile

    ledger = Ledger.objects.create(user=user_a, label="Vendor", account_class="current_liability")
    vp = VendorTaxProfile.objects.create(user=user_a, ledger=ledger, tin_encrypted=encrypt_secret("123456789"), tin_last4="6789")
    body = staff.get(f"/admin/ledger/vendortaxprofile/{vp.pk}/change/").content.decode()
    assert "123456789" in body and vp.tin_encrypted in body

    tok = GhlOAuthToken.objects.create(
        location_id="LOC1",
        company_id="C1",
        user_type="Location",
        access_token_encrypted=encrypt_secret("at-1"),
        refresh_token_encrypted=encrypt_secret("rt-1"),
        scope="a b",
        expires_at=timezone.now() + timedelta(days=1),
    )
    body = staff.get(f"/admin/ghl/ghloauthtoken/{tok.pk}/change/").content.decode()
    assert "at-1" in body and "rt-1" in body and tok.access_token_encrypted in body and "a b" in body


def test_decrypted_secrets_are_superuser_only(user_a):
    from django.contrib.auth.models import Permission

    item = PlaidItem.objects.create(user=user_a, item_id="it2", access_token_encrypted=encrypt_secret("super-secret-token"))
    staff_only = get_user_model().objects.create_user("staff@example.com", "Pass-w0rd-1!", is_staff=True)
    staff_only.user_permissions.set(Permission.objects.filter(content_type__app_label="banking"))
    c = Client()
    c.force_login(staff_only)
    body = c.get(f"/admin/banking/plaiditem/{item.pk}/change/").content.decode()
    assert "Superuser only" in body and "super-secret-token" not in body
    assert not AuditLog.objects.filter(action="admin_reveal_secret").exists()


def test_wrong_encryption_key_does_not_break_the_page(staff, user_a, settings):
    item = PlaidItem.objects.create(user=user_a, item_id="it3", access_token_encrypted=encrypt_secret("tok"))
    settings.TOKEN_ENCRYPTION_KEY = "a-different-key"
    page = staff.get(f"/admin/banking/plaiditem/{item.pk}/change/")
    assert page.status_code == 200 and "Could not decrypt" in page.content.decode()


def test_audit_log_is_read_only(staff, user_a):
    from apps.core.services import audit

    entry = audit(user_a, "test_action", "thing", "1")
    body = staff.get(f"/admin/core/auditlog/{entry.pk}/change/").content.decode()
    assert "test_action" in body and str(entry.pk) in body  # fully visible
    assert staff.post(f"/admin/core/auditlog/{entry.pk}/delete/", {"post": "yes"}).status_code == 403


def test_non_staff_cannot_open_admin(user_a):
    c = Client()
    c.force_login(user_a)
    assert c.get("/admin/").status_code == 302  # redirected to admin login
