#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.data_source_service import (
    prepare_source_config,
    prepare_source_config_update,
    public_source_config,
    resolved_source_config,
    migrate_plaintext_source_headers,
)
from app.auto_scan_api import validate_scan_type
from app.models import Base, DataSource
from app.source_connectors import request_api_source


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
