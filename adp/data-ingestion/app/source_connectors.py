#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from ftplib import FTP, FTP_TLS
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx
from sqlalchemy import create_engine, inspect as sqlalchemy_inspect, text
from sqlalchemy.engine import Engine


_DATE_TYPE_MARKERS = ("DATE", "TIME")
_INTEGER_TYPE_MARKERS = ("INT", "NUMBER", "NUMERIC", "DECIMAL")
_WATERMARK_NAME_GROUPS = (
    ("updated_at", "update_time", "last_updated", "last_modified", "modified_at", "changed_at"),
    ("created_at", "create_time", "created_time"),
    ("version",),
    ("id",),
)


def load_sftp_private_key(value: str):
    import paramiko

    for key_type in (
        paramiko.Ed25519Key,
        paramiko.RSAKey,
        paramiko.ECDSAKey,
        paramiko.DSSKey,
    ):
        try:
            return key_type.from_private_key(io.StringIO(value))
        except Exception:
            continue
    raise ValueError("SFTP 私钥格式无法识别")


def database_url(source_type: str, config: dict[str, Any]) -> str:
    username = quote_plus(str(config.get("username") or ""))
    password = quote_plus(str(config.get("password") or ""))
    host = str(config.get("host") or "127.0.0.1")
    if source_type == "mysql":
        port = int(config.get("port") or 3306)
        database = quote_plus(str(config.get("database") or ""))
        return (
            f"mysql+pymysql://{username}:{password}@{host}:{port}/{database}"
            "?charset=utf8mb4"
        )
    if source_type in {"postgres", "postgresql"}:
        port = int(config.get("port") or 5432)
        database = quote_plus(str(config.get("database") or ""))
        return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{database}"
    raise ValueError("数据源不是受支持的数据库类型")


def database_query_mode(config: dict[str, Any]) -> str:
    return "table"


def configured_table_names(config: dict[str, Any]) -> list[str]:
    values = config.get("table_names")
    if isinstance(values, list):
        names = list(
            dict.fromkeys(
                str(value).strip()
                for value in values
                if isinstance(value, str) and value.strip()
            )
        )
        if names:
            return names
    table_name = str(config.get("table_name") or "").strip()
    return [table_name] if table_name else []


def _column_type_name(column: dict[str, Any]) -> str:
    return str(column.get("type") or "").upper()


def _recommend_watermark(columns: list[dict[str, Any]]) -> dict[str, str]:
    by_name = {str(column["name"]).lower(): column for column in columns}
    for group_index, names in enumerate(_WATERMARK_NAME_GROUPS):
        for name in names:
            column = by_name.get(name)
            if column is None:
                continue
            type_name = _column_type_name(column)
            is_temporal = any(marker in type_name for marker in _DATE_TYPE_MARKERS)
            is_integer = any(marker in type_name for marker in _INTEGER_TYPE_MARKERS)
            if group_index < 3 and not (is_temporal or is_integer):
                continue
            if group_index == 3 and not is_integer:
                continue
            reasons = {
                0: "这是更新时间字段，适合同步新增和修改的数据。",
                1: "这是创建时间字段，适合只追加、不修改的数据。",
                2: "这是版本字段，适合版本号持续递增的数据。",
                3: "这是数字主键，适合只追加、不修改的数据。",
            }
            return {
                "watermark_column": str(column["name"]),
                "sync_mode": "incremental",
                "initial_cursor": "0" if is_integer else "1970-01-01T00:00:00",
                "reason": reasons[group_index],
            }
    return {
        "watermark_column": "",
        "sync_mode": "full",
        "initial_cursor": "",
        "reason": "没有找到可靠的更新字段，将先使用全量检查。",
    }


def _database_catalog_data(
    engine: Engine,
    config: dict[str, Any],
    *,
    table_name: str = "",
) -> dict[str, Any]:
    inspector = sqlalchemy_inspect(engine)
    schema_name = str(config.get("schema_name") or "").strip() or None
    tables = {
        name: "table" for name in inspector.get_table_names(schema=schema_name)
    }
    for name in inspector.get_view_names(schema=schema_name):
        tables.setdefault(name, "view")
    table_options = [
        {"name": name, "kind": kind}
        for name, kind in sorted(tables.items(), key=lambda item: item[0].lower())
    ]
    resolved_schema = schema_name or inspector.default_schema_name or ""
    if not table_name:
        return {
            "schema_name": resolved_schema,
            "tables": table_options,
            "table_name": "",
            "columns": [],
            "primary_keys": [],
            "recommended_watermark": "",
            "recommended_sync_mode": "full",
            "recommended_initial_cursor": "",
            "recommendation_reason": "",
            "generated_query": "",
        }
    if table_name not in tables:
        raise ValueError(f"数据库中不存在表或视图：{table_name}")

    primary_keys = list(
        (inspector.get_pk_constraint(table_name, schema=schema_name) or {}).get(
            "constrained_columns",
            [],
        )
    )
    raw_columns = inspector.get_columns(table_name, schema=schema_name)
    columns = [
        {
            "name": str(column["name"]),
            "type": str(column.get("type") or ""),
            "nullable": bool(column.get("nullable", True)),
            "primary_key": str(column["name"]) in primary_keys,
        }
        for column in raw_columns
    ]
    recommendation = _recommend_watermark(columns)
    generated_config = {
        **config,
        "query_mode": "table",
        "table_name": table_name,
        "sync_mode": recommendation["sync_mode"],
        "watermark_column": recommendation["watermark_column"],
    }
    return {
        "schema_name": resolved_schema,
        "tables": table_options,
        "table_name": table_name,
        "columns": columns,
        "primary_keys": primary_keys,
        "recommended_watermark": recommendation["watermark_column"],
        "recommended_sync_mode": recommendation["sync_mode"],
        "recommended_initial_cursor": recommendation["initial_cursor"],
        "recommendation_reason": recommendation["reason"],
        "generated_query": database_source_query(engine, generated_config),
    }


def inspect_database_catalog(
    source_type: str,
    config: dict[str, Any],
    *,
    table_name: str = "",
) -> dict[str, Any]:
    engine = create_engine(database_url(source_type, config), pool_pre_ping=True)
    try:
        return _database_catalog_data(engine, config, table_name=table_name)
    finally:
        engine.dispose()


def database_source_query(engine: Engine, config: dict[str, Any]) -> str:
    table_names = configured_table_names(config)
    table_name = str(config.get("table_name") or "").strip()
    if not table_name and len(table_names) == 1:
        table_name = table_names[0]
    if not table_name:
        raise ValueError("请选择要同步的数据库表")
    inspector = sqlalchemy_inspect(engine)
    schema_name = str(config.get("schema_name") or "").strip() or None
    table_names = set(inspector.get_table_names(schema=schema_name))
    table_names.update(inspector.get_view_names(schema=schema_name))
    if table_name not in table_names:
        raise ValueError(f"数据库中不存在表或视图：{table_name}")
    columns = {
        str(column["name"])
        for column in inspector.get_columns(table_name, schema=schema_name)
    }
    quote = engine.dialect.identifier_preparer.quote
    qualified_table = (
        f"{quote(schema_name)}.{quote(table_name)}"
        if schema_name
        else quote(table_name)
    )
    query = f"SELECT * FROM {qualified_table}"
    # Database sources are synchronized as complete snapshots. Keeping this
    # query free of cursor predicates lets stale configurations run consistently.
    return query


def _path_value(value: Any, path: str) -> Any:
    current = value
    for token in [part for part in path.split(".") if part]:
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list):
            current = current[int(token)]
        else:
            raise ValueError(f"API record_path 无法解析：{path}")
    return current


def _tabular_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value):
            return [dict(item) for item in value]
        return [{"value": item} for item in value]
    if isinstance(value, dict):
        record_lists = [
            item
            for item in value.values()
            if isinstance(item, list) and all(isinstance(record, dict) for record in item)
        ]
        if len(record_lists) == 1:
            return [dict(item) for item in record_lists[0]]
        return [value]
    return [{"value": value}]


def _cell_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
    return value


def records_to_csv(records: list[dict[str, Any]]) -> bytes:
    records = sorted(
        records,
        key=lambda record: json.dumps(
            record,
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        ),
    )
    columns = sorted(
        {
            str(key)
            for record in records
            for key in record
        }
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                column: _cell_value(record.get(column))
                for column in columns
            }
        )
    return buffer.getvalue().encode("utf-8-sig")


def request_api_source(
    config: dict[str, Any],
    *,
    source_name: str = "api",
) -> tuple[str, bytes, int]:
    url = str(config.get("url") or "").strip()
    if not url:
        raise ValueError("缺少 API URL")
    method = str(config.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH"}:
        raise ValueError("API 数据源只支持 GET、POST、PUT 或 PATCH")
    headers = config.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("API headers 必须是对象")
    body = config.get("body")
    pagination_type = str(config.get("pagination_type") or "none")
    page_param = str(config.get("page_param") or "page")
    page_size_param = str(config.get("page_size_param") or "page_size")
    page_size = int(config.get("page_size") or 100)
    max_pages = max(1, min(int(config.get("max_pages") or 1), 100))
    current_url = url
    records: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    for page_index in range(max_pages):
        kwargs: dict[str, Any] = {"headers": headers}
        page_number = int(config.get("start_page") or 1) + page_index
        if method == "GET":
            params = dict(body) if isinstance(body, dict) else {}
            if pagination_type == "page":
                params[page_param] = page_number
                params[page_size_param] = page_size
            kwargs["params"] = params
        elif body not in (None, ""):
            payload_body = dict(body) if isinstance(body, dict) else {"body": body}
            if pagination_type == "page":
                payload_body[page_param] = page_number
                payload_body[page_size_param] = page_size
            kwargs["json"] = payload_body
        response = httpx.request(
            method,
            current_url,
            timeout=float(config.get("timeout_seconds") or 30),
            follow_redirects=True,
            **kwargs,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type and not content_type.endswith("+json"):
            if pagination_type != "none":
                raise ValueError("HTTP API 分页目前只支持 JSON 响应")
            if "csv" in content_type:
                rows = list(csv.DictReader(io.StringIO(response.text)))
                return (
                    f"{Path(source_name).stem or 'api'}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.csv",
                    records_to_csv(rows),
                    len(rows),
                )
            if "text" in content_type:
                raise ValueError("HTTP API 当前只支持 JSON 或 CSV 数据，不支持普通文本")
            raise ValueError(
                f"HTTP API 返回了不支持的内容类型：{content_type or 'unknown'}"
            )

        response_payload = response.json()
        page_digest = json.dumps(
            response_payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if page_digest in seen_pages:
            break
        seen_pages.add(page_digest)
        payload = response_payload
        record_path = str(config.get("record_path") or "").strip()
        if record_path:
            payload = _path_value(payload, record_path)
        page_records = _tabular_records(payload)
        if not page_records:
            break
        records.extend(page_records)
        if pagination_type == "none":
            break
        if pagination_type == "page":
            if len(page_records) < page_size:
                break
            continue
        if pagination_type == "next_url":
            next_path = str(config.get("next_url_path") or "").strip()
            if not next_path:
                raise ValueError("下一页链接分页必须配置 next_url_path")
            next_url = _path_value(response_payload, next_path)
            if not next_url:
                break
            current_url = urljoin(current_url, str(next_url))
            continue
        raise ValueError(f"不支持的 API 分页方式：{pagination_type}")

    return (
        f"{Path(source_name).stem or 'api'}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.csv",
        records_to_csv(records),
        len(records),
    )


def _test_ftp(
    source_type: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    host = str(config.get("host") or "").strip()
    if not host:
        raise ValueError("缺少 FTP/SFTP 主机地址")
    port = int(config.get("port") or (22 if source_type == "sftp" else 21))
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    private_key = str(config.get("private_key") or "")
    remote_path = str(config.get("path") or ".")
    if source_type == "sftp":
        import paramiko

        transport = paramiko.Transport((host, port))
        try:
            transport.connect(
                username=username,
                password=password if not private_key else None,
                pkey=load_sftp_private_key(private_key) if private_key else None,
            )
            client = paramiko.SFTPClient.from_transport(transport)
            try:
                names = client.listdir(remote_path)
            finally:
                client.close()
        finally:
            transport.close()
        return {"connected": True, "files": len(names), "path": remote_path}

    ftp = FTP_TLS() if config.get("use_tls") else FTP()
    try:
        ftp.connect(host, port, timeout=15)
        ftp.login(username or "anonymous", password or "")
        if isinstance(ftp, FTP_TLS):
            ftp.prot_p()
        ftp.cwd(remote_path)
        names = ftp.nlst()
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return {"connected": True, "files": len(names), "path": remote_path}


def _test_database(
    source_type: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    engine = create_engine(database_url(source_type, config), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            table_names = configured_table_names(config)
            if table_names:
                available = False
                for table_name in table_names:
                    query = database_source_query(
                        engine,
                        {**config, "table_name": table_name},
                    )
                    if connection.execute(text(query)).fetchone() is not None:
                        available = True
                return {
                    "connected": True,
                    "sample_available": available,
                    "tables": table_names,
                }
            raise ValueError("数据库数据源必须选择数据表")
    finally:
        engine.dispose()
    return {"connected": True, "sample_available": False}


def test_source_connection(
    source_type: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    if source_type in {"upload", "local_dir"}:
        configured_path = str(config.get("path") or "").strip()
        if source_type == "local_dir" and not configured_path:
            raise ValueError("本地目录数据源需要填写目录")
        if configured_path:
            path = Path(configured_path).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"目录不存在：{path}")
            if source_type == "local_dir" and not path.is_dir():
                raise ValueError(f"本地目录数据源必须指向目录：{path}")
        return {"connected": True, "path": configured_path or "."}
    if source_type in {"ftp", "sftp"}:
        return _test_ftp(source_type, config)
    if source_type in {"mysql", "postgres", "postgresql"}:
        return _test_database(source_type, config)
    if source_type == "api":
        filename, content, rows = request_api_source(
            config,
            source_name=str(config.get("name") or "api"),
        )
        return {
            "connected": True,
            "records": rows,
            "filename": filename,
            "bytes": len(content),
        }
    raise ValueError(f"不支持的数据源类型：{source_type}")
