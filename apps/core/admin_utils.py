"""Shared Django-admin helpers: owner-level visibility with audited secret reveal."""

from contextvars import ContextVar

from apps.core.crypto import decrypt_secret
from apps.core.services import audit

_current_request: ContextVar = ContextVar("admin_request", default=None)


class OwnerAdminMixin:
    """Shows every column (including id and timestamps) and exposes decrypted secrets to superusers.

    Encrypted columns themselves stay read-only (editing ciphertext by hand would corrupt it) but are always displayed.
    """

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        names = {f.name for f in self.model._meta.fields}
        for name in ("id", "created_at", "updated_at"):
            if name in names and name not in readonly:
                readonly.append(name)
        return readonly

    def changeform_view(self, request, *args, **kwargs):
        token = _current_request.set(request)  # lets the *_decrypted display methods see who is looking
        try:
            response = super().changeform_view(request, *args, **kwargs)
            if hasattr(response, "render") and not response.is_rendered:
                response.render()  # TemplateResponses render lazily; do it while the request context is set
            return response
        finally:
            _current_request.reset(token)


def reveal_secret(obj, attr: str) -> str:
    """Decrypted value of an encrypted column. Superusers only; every reveal is written to the audit log."""
    request = _current_request.get()
    if obj is None or obj.pk is None or not getattr(obj, attr, None):
        return "—"
    if request is None or not request.user.is_superuser:
        return "Superuser only"
    try:
        value = decrypt_secret(getattr(obj, attr))
    except Exception:  # noqa: BLE001 - wrong key / corrupted value must not break the page
        return "Could not decrypt (TOKEN_ENCRYPTION_KEY mismatch or corrupted value)"
    audit(request.user, "admin_reveal_secret", obj._meta.label_lower, str(obj.pk), {"field": attr})
    return value
