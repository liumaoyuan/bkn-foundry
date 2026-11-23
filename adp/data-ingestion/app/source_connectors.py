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
from sqlalchemy import create_engine, text


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
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def records_to_csv(records: list[dict[str, Any]]) -> bytes:
    columns: list[str] = []
    for record in records:
        for key in record:
            if key not in columns:
                columns.append(str(key))
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
                    response.text.encode("utf-8-sig"),
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
    query = str(config.get("query") or "").strip()
    try:
        with engine.connect() as connection:
            if query:
                params = {}
                if ":cursor" in query:
                    params["cursor"] = "1970-01-01T00:00:00"
                result = connection.execute(text(query), params)
                row = result.fetchone()
                return {"connected": True, "sample_available": row is not None}
            connection.execute(text("SELECT 1"))
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
