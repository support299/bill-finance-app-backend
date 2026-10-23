import shutil
import subprocess

import pytest

from apps.core.crypto import decrypt_secret, encrypt_secret

NODE_ENCRYPT = r"""
const { createCipheriv, createHash, randomBytes } = require("crypto");
const key = createHash("sha256").update(process.argv[1]).digest();
const iv = randomBytes(12);
const c = createCipheriv("aes-256-gcm", key, iv);
const enc = Buffer.concat([c.update(process.argv[2], "utf8"), c.final()]);
console.log([iv.toString("base64"), c.getAuthTag().toString("base64"), enc.toString("base64")].join("."));
"""


def test_roundtrip():
    assert decrypt_secret(encrypt_secret("access-sandbox-123")) == "access-sandbox-123"


def test_tamper_detected():
    import base64

    from cryptography.exceptions import InvalidTag

    iv, tag, data = encrypt_secret("sensitive-token").split(".")
    flipped = bytearray(base64.b64decode(data))
    flipped[0] ^= 0x01  # flip one bit of the ciphertext
    with pytest.raises(InvalidTag):
        decrypt_secret(f"{iv}.{tag}.{base64.b64encode(bytes(flipped)).decode()}")
    with pytest.raises(ValueError):
        decrypt_secret("not-a-valid-payload")


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_decrypts_values_encrypted_by_the_original_node_code():
    out = subprocess.run(["node", "-e", NODE_ENCRYPT, "test-encryption-key", "plaid-token-xyz"], capture_output=True, text=True, check=True)
    assert decrypt_secret(out.stdout.strip()) == "plaid-token-xyz"
