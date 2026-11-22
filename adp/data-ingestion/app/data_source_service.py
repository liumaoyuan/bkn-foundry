from __future__ import annotations

from typing import Any

from .models import DataSource
from .secret_store import decrypt_secret, encrypt_secret


SECRET_KEYS = {"password", "api_key", "secret"}


def prepare_source_config(config: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(config)
    for key in SECRET_KEYS:
        value = prepared.pop(key, None)
        if value:
            prepared[f"{key}_encrypted"] = encrypt_secret(str(value))
    return prepared


def public_source_config(config: dict[str, Any]) -> dict[str, Any]:
    public = dict(config or {})
    for key in SECRET_KEYS:
        public.pop(key, None)
        public.pop(f"{key}_encrypted", None)
        if config.get(f"{key}_encrypted") or config.get(key):
            public[f"has_{key}"] = True
    return public


def resolved_source_config(source: DataSource) -> dict[str, Any]:
    resolved = dict(source.config or {})
    for key in SECRET_KEYS:
        encrypted = resolved.pop(f"{key}_encrypted", "")
        if encrypted:
            resolved[key] = decrypt_secret(encrypted)
    return resolved
