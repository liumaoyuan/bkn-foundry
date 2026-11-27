#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.data_source_service import (
    prepare_source_config,
    prepare_source_config_update,
    public_source_config,
    resolved_source_config,
    migrate_plaintext_source_headers,
    validate_source_config,
)
from app.auto_scan_api import validate_scan_requirements, validate_scan_type
from app.models import Base, DataSource
from app.source_connectors import (
    _database_catalog_data,
    configured_table_names,
    database_source_query,
    request_api_source,
)


class SourceConnectorTest(unittest.TestCase):
    def test_api_json_records_are_converted_to_csv(self) -> None:
        request = httpx.Request("GET", "https://api.example.test/items")
        response = httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            json={"data": {"items": [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]}},
        )
        with patch("app.source_connectors.httpx.request", return_value=response):
            filename, content, count = request_api_source(
                {
                    "url": "https://api.example.test/items",
                    "record_path": "data.items",
                    "headers": {"Authorization": "Bearer test"},
                },
                source_name="Marine API",
            )

        self.assertTrue(filename.endswith(".csv"))
        self.assertIn("id,name", content.decode("utf-8-sig"))
        self.assertIn("alpha", content.decode("utf-8-sig"))
        self.assertEqual(count, 2)

    def test_api_page_pagination_collects_all_pages(self) -> None:
        def request(_method: str, _url: str, **kwargs):
            page = kwargs["params"]["page"]
            payload = (
                {"data": [{"id": 1}, {"id": 2}]}
                if page == 1
                else {"data": [{"id": 3}]}
            )
            return httpx.Response(
                200,
                request=httpx.Request("GET", _url),
                headers={"content-type": "application/json"},
                json=payload,
            )

        with patch("app.source_connectors.httpx.request", side_effect=request):
            _, content, count = request_api_source(
                {
                    "url": "https://api.example.test/items",
                    "record_path": "data",
                    "pagination_type": "page",
                    "page_size": 2,
                    "max_pages": 5,
                },
                source_name="Paged API",
            )
        self.assertEqual(count, 3)
        self.assertIn("id\r\n1\r\n2\r\n3", content.decode("utf-8-sig"))

    def test_api_row_order_does_not_change_snapshot_bytes(self) -> None:
        responses = [
            [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}],
            [{"name": "beta", "id": 2}, {"name": "alpha", "id": 1}],
        ]

        def request(_method: str, _url: str, **_kwargs):
            payload = responses.pop(0)
            return httpx.Response(
                200,
                request=httpx.Request("GET", _url),
                headers={"content-type": "application/json"},
                json={"data": payload},
            )

        with patch("app.source_connectors.httpx.request", side_effect=request):
            _, first, _ = request_api_source(
                {
                    "url": "https://api.example.test/items",
                    "record_path": "data",
                }
            )
            _, second, _ = request_api_source(
                {
                    "url": "https://api.example.test/items",
                    "record_path": "data",
                }
            )

        self.assertEqual(first, second)

    def test_update_keeps_existing_encrypted_password_when_blank(self) -> None:
        current = {"password_encrypted": "encrypted-value", "query": "select 1"}
        updated = prepare_source_config_update(
            current,
            {"query": "select 2", "password": ""},
        )
        self.assertEqual(updated["password_encrypted"], "encrypted-value")
        self.assertEqual(updated["query"], "select 2")

    def test_api_headers_are_encrypted_and_hidden_from_public_config(self) -> None:
        prepared = prepare_source_config(
            {"headers": {"Authorization": "Bearer top-secret"}}
        )
        self.assertNotIn("headers", prepared)
        self.assertIn("headers_encrypted", prepared)
        public = public_source_config(prepared)
        self.assertNotIn("top-secret", str(public))
        self.assertEqual(public["header_names"], ["Authorization"])
        source = DataSource(name="API", source_type="api", config=prepared)
        self.assertEqual(
            resolved_source_config(source)["headers"]["Authorization"],
            "Bearer top-secret",
        )

    def test_api_source_is_valid_for_api_auto_scan(self) -> None:
        source = DataSource(name="API", source_type="api", config={})
        validate_scan_type(source, "api")

    def test_all_supported_source_types_map_to_auto_scan(self) -> None:
        accepted = {
            "upload": "local",
            "local_dir": "local",
            "ftp": "ftp",
            "sftp": "sftp",
            "mysql": "database",
            "postgres": "database",
            "postgresql": "database",
            "api": "api",
        }
        for source_type, scan_type in accepted.items():
            with self.subTest(source_type=source_type):
                source = DataSource(
                    name=source_type,
                    source_type=source_type,
                    config={},
                )
                validate_scan_type(source, scan_type)

    def test_database_catalog_builds_full_snapshot_query(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE orders ("
                    "id INTEGER PRIMARY KEY, "
                    "name TEXT, "
                    "updated_at DATETIME NOT NULL)"
                )
            )

        catalog = _database_catalog_data(engine, {}, table_name="orders")
        self.assertEqual(catalog["recommended_watermark"], "updated_at")
        self.assertEqual(catalog["recommended_sync_mode"], "incremental")
        query = database_source_query(
            engine,
            {
                "query_mode": "table",
                "table_name": "orders",
                "sync_mode": "incremental",
                "watermark_column": "updated_at",
            },
        )
        self.assertIn("FROM orders", query)
        self.assertNotIn(":cursor", query)
        self.assertNotIn("WHERE", query)
        engine.dispose()

    def test_table_mode_source_is_valid_without_manual_sql(self) -> None:
        source = DataSource(
            name="Orders",
            source_type="mysql",
            config={
                "query_mode": "table",
                "table_name": "orders",
                "sync_mode": "incremental",
                "watermark_column": "updated_at",
            },
        )
        validate_scan_requirements(source, "database", "")

    def test_database_source_binds_exactly_one_table(self) -> None:
        config = {
            "query_mode": "table",
            "table_name": "customers",
        }
        validate_source_config("mysql", config)
        self.assertEqual(configured_table_names(config), ["customers"])

        config = {
            "query_mode": "table",
            "table_names": ["customers", "orders", "customers"],
        }
        with self.assertRaisesRegex(ValueError, "只能绑定一张表"):
            validate_source_config("mysql", config)

    def test_unsupported_source_and_custom_sql_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "不支持的数据源类型"):
            validate_source_config("unknown", {})
        with self.assertRaisesRegex(ValueError, "本地目录"):
            validate_source_config("local_dir", {})
        with self.assertRaisesRegex(ValueError, "主机地址"):
            validate_source_config("ftp", {})
        with self.assertRaisesRegex(ValueError, "接口地址"):
            validate_source_config("api", {})
        with self.assertRaisesRegex(ValueError, "必须选择数据表"):
            validate_source_config(
                "mysql",
                {"query_mode": "custom", "query": "SELECT 1"},
            )

    def test_table_mode_requires_a_table(self) -> None:
        with self.assertRaisesRegex(ValueError, "请选择需要同步的数据表"):
            validate_source_config(
                "mysql",
                {"query_mode": "table", "sync_mode": "full"},
            )

    def test_startup_migration_encrypts_existing_plain_headers(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            source = DataSource(
                name="legacy API",
                source_type="api",
                config={"headers": {"Authorization": "Bearer old"}},
            )
            db.add(source)
            db.commit()
            self.assertEqual(migrate_plaintext_source_headers(db), 1)
            db.refresh(source)
            self.assertNotIn("headers", source.config)
            self.assertIn("headers_encrypted", source.config)


if __name__ == "__main__":
    unittest.main()
