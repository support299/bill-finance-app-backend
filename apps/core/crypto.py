"""AES-256-GCM secret encryption.

Output format is base64(iv).base64(tag).base64(ciphertext) with key = sha256(TOKEN_ENCRYPTION_KEY),
which matches the original app so previously encrypted values stay decryptable with the same key.
"""

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings


def _key() -> bytes:
    if not settings.TOKEN_ENCRYPTION_KEY:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    return hashlib.sha256(settings.TOKEN_ENCRYPTION_KEY.encode()).digest()


def encrypt_secret(plain: str) -> str:
    iv = os.urandom(12)
    sealed = AESGCM(_key()).encrypt(iv, plain.encode(), None)
    ciphertext, tag = sealed[:-16], sealed[-16:]
    return ".".join(base64.b64encode(p).decode() for p in (iv, tag, ciphertext))


def decrypt_secret(payload: str) -> str:
    try:
        iv, tag, data = (base64.b64decode(p) for p in payload.split("."))
    except ValueError as exc:
        raise ValueError("Malformed encrypted value") from exc
    return AESGCM(_key()).decrypt(iv, data + tag, None).decode()
