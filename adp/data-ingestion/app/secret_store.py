from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from .config import DATA_SOURCE_SECRET_KEY


def _fernet() -> Fernet:
    digest = hashlib.sha256(DATA_SOURCE_SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
