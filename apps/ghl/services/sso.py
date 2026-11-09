"""GoHighLevel SSO for the embedded custom page.

The page asks its parent frame who is signed in (postMessage ``REQUEST_USER_DATA``); GoHighLevel answers with a payload
encrypted with the app's shared secret (Marketplace -> Advanced settings -> Auth). That is CryptoJS ``AES.encrypt(text,
passphrase)``: OpenSSL's ``Salted__`` envelope, key and IV from EVP_BytesToKey (MD5), AES-256-CBC, PKCS#7 padding.

Decrypted it names the user, their agency, their role and the sub-account the page is open in::

    {"userId": "...", "companyId": "...", "role": "admin" | "user", "type": "agency" | "location",
     "activeLocation": "...", "userName": "...", "email": "..."}
"""

import base64
import hashlib
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.utils import timezone

from ..models import GhlLocation, GhlMember

ACCESS_LEVELS = ("none", "viewer", "editor", "admin")


def _evp_bytes_to_key(secret: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> tuple[bytes, bytes]:
    out, block = b"", b""
    while len(out) < key_len + iv_len:
        block = hashlib.md5(block + secret + salt).digest()
        out += block
    return out[:key_len], out[key_len : key_len + iv_len]


def decrypt_user_data(payload: str, secret: str) -> dict:
    """Raises ValueError for anything that is not a payload encrypted with ``secret``."""
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("not base64") from exc
    if len(raw) < 32 or raw[:8] != b"Salted__":
        raise ValueError("not an OpenSSL salted payload")
    key, iv = _evp_bytes_to_key(secret.encode(), raw[8:16])
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(raw[16:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        data = unpadder.update(padded) + unpadder.finalize()
        obj = json.loads(data.decode())
    except Exception as exc:  # noqa: BLE001 - wrong secret, tampered bytes, not JSON
        raise ValueError("payload does not decrypt") from exc
    if not isinstance(obj, dict):
        raise ValueError("payload is not an object")
    return obj


def encrypt_user_data(data: dict, secret: str) -> str:
    """The inverse, byte-compatible with CryptoJS; used by tests and local tooling."""
    salt = os.urandom(8)
    key, iv = _evp_bytes_to_key(secret.encode(), salt)
    padder = padding.PKCS7(128).padder()
    body = padder.update(json.dumps(data).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(b"Salted__" + salt + encryptor.update(body) + encryptor.finalize()).decode()


@dataclass(frozen=True)
class SsoUser:
    user_id: str
    company_id: str
    type: str  # agency | location
    role: str  # admin | user
    active_location: str | None
    name: str | None
    email: str | None

    def actor(self, location_id: str) -> dict:
        """The token claim that follows this person through the session."""
        return {
            "uid": self.user_id,
            "name": self.name,
            "email": self.email,
            "type": self.type,
            "role": self.role,
            "loc": location_id,
            "co": self.company_id,
        }


def parse_user(obj: dict) -> SsoUser:
    user_id = str(obj.get("userId") or "")
    company_id = str(obj.get("companyId") or "")
    type_ = str(obj.get("type") or "location").lower()
    role = str(obj.get("role") or "user").lower()
    if not user_id or not company_id or type_ not in ("agency", "location") or role not in ("admin", "user"):
        raise ValueError("payload is missing user, company, type or role")
    active = obj.get("activeLocation")
    return SsoUser(
        user_id=user_id,
        company_id=company_id,
        type=type_,
        role=role,
        active_location=str(active) if active else None,
        name=(str(obj.get("userName") or "").strip() or None),
        email=(str(obj.get("email") or "").strip().lower() or None),
    )


def default_access(ghl_type: str | None, ghl_role: str | None) -> str:
    """Agency staff administer every sub-account; a sub-account's own admins edit; its other users have no access."""
    if ghl_type == "agency":
        return "admin"
    if ghl_role == "admin":
        return "editor"
    return "none"


def effective_access(location_id: str, ghl_user_id: str, ghl_type: str | None = None, ghl_role: str | None = None) -> str:
    member = (
        GhlMember.objects.filter(location__location_id=location_id, ghl_user_id=ghl_user_id)
        .only("access", "ghl_type", "ghl_role")
        .first()
    )
    if member and member.access:
        return member.access
    return default_access(ghl_type or (member.ghl_type if member else None), ghl_role or (member.ghl_role if member else None))


def record_member(loc: GhlLocation, sso: SsoUser) -> GhlMember:
    """Remembers who signed in (name, email, role) so administrators can see and manage them."""
    member, _ = GhlMember.objects.get_or_create(location=loc, ghl_user_id=sso.user_id)
    member.name, member.email, member.ghl_type, member.ghl_role = sso.name, sso.email, sso.type, sso.role
    member.last_seen_at = timezone.now()
    member.save(update_fields=["name", "email", "ghl_type", "ghl_role", "last_seen_at"])
    return member
