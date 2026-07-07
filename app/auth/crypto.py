from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config import auth_settings

_FERNET_SALT = b"text2sql-app-v1"


def _fernet() -> Fernet:
    secret = auth_settings.app_secret_key.encode("utf-8")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_FERNET_SALT, iterations=120_000)
    key = base64.urlsafe_b64encode(kdf.derive(secret))
    return Fernet(key)


def encrypt_payload(payload: dict[str, Any]) -> bytes:
    token = _fernet().encrypt(json.dumps(payload).encode("utf-8"))
    return token


def decrypt_payload(encrypted: bytes) -> dict[str, Any]:
    try:
        raw = _fernet().decrypt(encrypted)
    except InvalidToken as exc:
        raise ValueError("Could not decrypt payload") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid decrypted payload")
    return data
