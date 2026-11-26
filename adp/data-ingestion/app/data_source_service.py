from __future__ import annotations

import json
from typing import Any

from .models import DataSource
from .secret_store import decrypt_secret, encrypt_secret
from .source_connectors import configured_table_names


SECRET_KEYS = {"password", "api_key", "secret", "private_key"}
SUPPORTED_SOURCE_TYPES = {
    "upload",
    "local_dir",
    "ftp",
    "sftp",
    "mysql",
    "postgres",
    "postgresql",
    "api",
}


def validate_source_config(source_type: str, config: dict[str, Any]) -> None:
    if source_type not in SUPPORTED_SOURCE_TYPES:
        raise ValueError(f"不支持的数据源类型：{source_type}")
    if source_type == "local_dir" and not str(
        config.get("path") or ""
    ).strip():
        raise ValueError("本地目录数据源需要填写目录")
    if source_type in {"ftp", "sftp"} and not str(
        config.get("host") or ""
    ).strip():
        raise ValueError("FTP/SFTP 数据源需要填写主机地址")
    if source_type == "api":
        if not str(config.get("url") or config.get("host") or "").strip():
            raise ValueError("HTTP API 数据源需要填写接口地址")
    if source_type not in {"mysql", "postgres", "postgresql"}:
        return
    if str(config.get("query_mode") or "table") != "table":
        raise ValueError("数据库数据源必须选择数据表")
    if not configured_table_names(config):
        raise ValueError("请选择需要同步的数据表")


def prepare_source_config(config: dict[str, Any]) -> dict[str, Any]:
    prepared = {
        key: value
        for key, value in config.items()
        if not key.startswith("has_")
    }
    for key in SECRET_KEYS:
        value = prepared.pop(key, None)
        if value:
            prepared[f"{key}_encrypted"] = encrypt_secret(str(value))
    headers = prepared.pop("headers", None)
    if isinstance(headers, dict) and headers:
        prepared["headers_encrypted"] = encrypt_secret(
            json.dumps(headers, ensure_ascii=False)
        )
    return prepared


def prepare_source_config_update(
    current: dict[str, Any],
    changed: dict[str, Any],
) -> dict[str, Any]:
    prepared = {
        key: value
        for key, value in changed.items()
        if not key.startswith("has_")
    }
    for key in SECRET_KEYS:
        if prepared.get(key):
            continue
        prepared.pop(key, None)
        encrypted = (current or {}).get(f"{key}_encrypted")
        if encrypted:
            prepared[f"{key}_encrypted"] = encrypted
    if not prepared.get("headers"):
        prepared.pop("headers", None)
        if (current or {}).get("headers_encrypted"):
            prepared["headers_encrypted"] = current["headers_encrypted"]
        elif (current or {}).get("headers"):
            prepared["headers"] = current["headers"]
    if "query" in prepared:
        prepared["query"] = str(prepared["query"]).strip()
    return prepare_source_config(prepared)


def public_source_config(config: dict[str, Any]) -> dict[str, Any]:
    public = dict(config or {})
    header_names: list[str] = []
    headers: dict[str, Any] = {}
    try:
        if config.get("headers_encrypted"):
            headers = json.loads(decrypt_secret(config["headers_encrypted"]))
        elif isinstance(config.get("headers"), dict):
            headers = config["headers"]
        header_names = sorted(str(name) for name in headers)
    except Exception:
        header_names = []
    public.pop("headers", None)
    public.pop("headers_encrypted", None)
    if header_names:
        public["has_headers"] = True
        public["header_names"] = header_names
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
    headers_encrypted = resolved.pop("headers_encrypted", "")
    if headers_encrypted:
        resolved["headers"] = json.loads(decrypt_secret(headers_encrypted))
    return resolved


def migrate_plaintext_source_headers(db) -> int:
    changed = 0
    for source in db.query(DataSource).all():
        if "headers" not in (source.config or {}):
            continue
        source.config = prepare_source_config(source.config)
        changed += 1
    if changed:
        db.commit()
    return changed


def migrate_plaintext_mcp_headers(db) -> int:
    from .models import McpToolDefinition

    changed = 0
    for tool in db.query(McpToolDefinition).all():
        if "headers" not in (tool.config or {}):
            continue
        tool.config = prepare_source_config(tool.config)
        changed += 1
    if changed:
        db.commit()
    return changed
