# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.engine import make_url

from .models import DataSpace
from .secret_store import decrypt_secret


DEFAULT_GATEWAY_URL = "http://gateway"
DATABASE_CONNECTORS = {"mysql", "mariadb"}


class CatalogSyncError(RuntimeError):
    pass


def _database_connection(space: DataSpace) -> dict[str, Any]:
    database_url = decrypt_secret(space.database_url_encrypted)
    parsed = make_url(database_url)
    connector_type = parsed.get_backend_name().lower()
    if connector_type not in DATABASE_CONNECTORS:
        raise CatalogSyncError("只有 MySQL/MariaDB 数据空间可以同步到数据目录")
    database_name = (parsed.database or "").strip()
    host = (parsed.host or "").strip()
    username = (parsed.username or "").strip()
    if not database_name or not host or not username:
        raise CatalogSyncError("数据空间连接信息不完整")
    return {
        "connector_config": {
            "database": database_name,
            "host": host,
            "password": parsed.password or "",
            "port": parsed.port or 3306,
            "username": username,
        },
        "connector_type": connector_type,
        "database_name": database_name,
        "host": host,
        "port": parsed.port or 3306,
    }


def _matches(connection: dict[str, Any], catalog: dict[str, Any]) -> bool:
    config = catalog.get("connector_config")
    if not isinstance(config, dict):
        return False
    database = str(config.get("database") or "").strip().lower()
    host = str(config.get("host") or "").strip().lower()
    try:
        port = int(config.get("port") or connection["port"])
    except (TypeError, ValueError):
        port = connection["port"]
    return (
        database == connection["database_name"].lower()
        and (not host or host == connection["host"].lower())
        and port == connection["port"]
    )


async def _json_response(response: httpx.Response) -> Any:
    if response.is_success:
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise CatalogSyncError("Vega 返回了无效响应") from error
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("description") or body.get("message") or "")
    except ValueError:
        detail = response.text[:300]
    raise CatalogSyncError(detail or f"Vega 请求失败（HTTP {response.status_code}）")


async def sync_space_catalog(
    space: DataSpace,
    *,
    access_token: str,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    token = access_token.strip()
    if not token:
        raise CatalogSyncError("当前请求缺少访问令牌，无法同步数据目录")
    connection = _database_connection(space)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=gateway_url.rstrip("/"),
        headers=headers,
        timeout=30,
        transport=transport,
    ) as client:
        response = await client.get(
            "/api/vega-backend/v1/catalogs",
            params={"type": "physical", "limit": 200, "offset": 0},
        )
        payload = await _json_response(response)
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise CatalogSyncError("Vega 目录列表格式无效")

        existing: dict[str, Any] | None = None
        for summary in entries:
            if not isinstance(summary, dict):
                continue
            catalog_id = str(summary.get("id") or "").strip()
            connector_type = str(summary.get("connector_type") or "").strip().lower()
            if not catalog_id or connector_type not in DATABASE_CONNECTORS:
                continue
            detail = await _json_response(
                await client.get(f"/api/vega-backend/v1/catalogs/{catalog_id}")
            )
            if isinstance(detail, dict) and _matches(connection, detail):
                existing = detail
                break

        created = existing is None
        if created:
            created_payload = await _json_response(
                await client.post(
                    "/api/vega-backend/v1/catalogs",
                    params={"allow_unhealthy": "true"},
                    json={
                        "connector_config": connection["connector_config"],
                        "connector_type": connection["connector_type"],
                        "description": f"数据入库连接：{space.name}",
                        "enabled": True,
                        "name": connection["database_name"],
                        "tags": ["data-ingestion", "managed"],
                    },
                )
            )
            catalog_id = (
                str(created_payload.get("id") or "").strip()
                if isinstance(created_payload, dict)
                else ""
            )
            if not catalog_id:
                raise CatalogSyncError("Vega 未返回新建目录 ID")
        else:
            catalog_id = str(existing.get("id") or "").strip()

        discovery_started = False
        try:
            await _json_response(
                await client.post(
                    f"/api/vega-backend/v1/catalogs/{catalog_id}/discover",
                    params={"wait": "false"},
                    json={"strategy": "full_sync"},
                )
            )
            discovery_started = True
        except CatalogSyncError:
            # The catalog binding is still useful when a transient discovery
            # failure occurs; ingestion completion will retry the sync.
            discovery_started = False

        return {
            "catalog_id": catalog_id,
            "created": created,
            "discovery_started": discovery_started,
            "database_label": space.database_label,
        }
