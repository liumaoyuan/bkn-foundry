# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import json
import unittest

import httpx

from app.catalog_sync import sync_space_catalog
from app.models import DataSpace
from app.secret_store import encrypt_secret


class CatalogSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_matching_catalog_and_starts_discovery(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/catalogs") and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {"id": "catalog-bkn", "connector_type": "mysql"}
                        ],
                        "total_count": 1,
                    },
                )
            if request.url.path.endswith("/catalogs/catalog-bkn"):
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "id": "catalog-bkn",
                                "name": "bkn",
                                "connector_type": "mysql",
                                "connector_config": {
                                    "database": "bkn",
                                    "host": "db.example.com",
                                    "port": 3306,
                                },
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/discover"):
                return httpx.Response(200, json={"id": "discover-1"})
            return httpx.Response(404)

        space = DataSpace(
            id="space-1",
            name="Default",
            slug="default",
            database_url_encrypted=encrypt_secret(
                "mysql+pymysql://reader:secret@db.example.com:3306/bkn"
            ),
            database_label="mysql://db.example.com:3306/bkn",
            is_default=True,
            status="ready",
        )
        result = await sync_space_catalog(
            space,
            access_token="access-token",
            gateway_url="http://gateway.test",
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(result["catalog_id"], "catalog-bkn")
        self.assertFalse(result["created"])
        self.assertTrue(result["discovery_started"])
        self.assertEqual(
            [request.method for request in requests],
            ["GET", "GET", "POST"],
        )
        self.assertEqual(
            json.loads(requests[-1].content),
            {"strategy": "full_sync"},
        )

    async def test_creates_catalog_when_no_connection_matches(self) -> None:
        created_body: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/catalogs") and request.method == "GET":
                return httpx.Response(200, json={"entries": [], "total_count": 0})
            if request.url.path.endswith("/catalogs") and request.method == "POST":
                created_body.update(json.loads(request.content))
                return httpx.Response(201, json={"id": "catalog-new"})
            if request.url.path.endswith("/discover"):
                return httpx.Response(200, json={"id": "discover-1"})
            return httpx.Response(404)

        space = DataSpace(
            id="space-2",
            name="Customer data",
            slug="customer",
            database_url_encrypted=encrypt_secret(
                "mariadb+pymysql://reader:secret@mariadb.example.com:3307/customer_db"
            ),
            database_label="mysql://mariadb.example.com:3307/customer_db",
            is_default=False,
            status="ready",
        )
        result = await sync_space_catalog(
            space,
            access_token="access-token",
            gateway_url="http://gateway.test",
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(result["catalog_id"], "catalog-new")
        self.assertTrue(result["created"])
        self.assertEqual(created_body["connector_type"], "mariadb")
        self.assertEqual(created_body["name"], "customer_db")
        self.assertEqual(created_body["connector_config"]["port"], 3307)

    async def test_reuses_same_named_catalog_when_detail_omits_config(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/catalogs") and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {"id": "catalog-bkn", "name": "bkn", "connector_type": "mysql"}
                        ]
                    },
                )
            if request.url.path.endswith("/catalogs/catalog-bkn"):
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "id": "catalog-bkn",
                                "name": "bkn",
                                "connector_type": "mysql",
                                "connector_config": {},
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/discover"):
                return httpx.Response(200, json={"id": "discover-1"})
            return httpx.Response(404)

        space = DataSpace(
            id="space-1",
            name="Default",
            slug="default",
            database_url_encrypted=encrypt_secret(
                "mysql+pymysql://reader:secret@db.example.com:3306/bkn"
            ),
            database_label="mysql://db.example.com:3306/bkn",
            is_default=True,
            status="ready",
        )
        result = await sync_space_catalog(
            space,
            access_token="access-token",
            gateway_url="http://gateway.test",
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(result["catalog_id"], "catalog-bkn")
        self.assertFalse(result["created"])
        self.assertFalse(
            any(
                request.url.path.endswith("/catalogs") and request.method == "POST"
                for request in requests
            )
        )

    async def test_recovers_existing_catalog_after_duplicate_name_error(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/catalogs") and request.method == "GET":
                if request.url.params.get("name") == "bkn":
                    return httpx.Response(
                        200,
                        json={
                            "entries": [
                                {"id": "catalog-bkn", "name": "bkn", "connector_type": "mysql"}
                            ]
                        },
                    )
                return httpx.Response(200, json={"entries": []})
            if request.url.path.endswith("/catalogs") and request.method == "POST":
                return httpx.Response(409, json={"description": "目录名称已存在"})
            if request.url.path.endswith("/catalogs/catalog-bkn"):
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "id": "catalog-bkn",
                                "name": "bkn",
                                "connector_type": "mysql",
                                "connector_config": {
                                    "database": "bkn",
                                    "host": "db.example.com",
                                    "port": 3306,
                                },
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/discover"):
                return httpx.Response(200, json={"id": "discover-1"})
            return httpx.Response(404)

        space = DataSpace(
            id="space-1",
            name="Default",
            slug="default",
            database_url_encrypted=encrypt_secret(
                "mysql+pymysql://reader:secret@db.example.com:3306/bkn"
            ),
            database_label="mysql://db.example.com:3306/bkn",
            is_default=True,
            status="ready",
        )
        result = await sync_space_catalog(
            space,
            access_token="access-token",
            gateway_url="http://gateway.test",
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(result["catalog_id"], "catalog-bkn")
        self.assertFalse(result["created"])
        self.assertTrue(result["discovery_started"])

    async def test_resolves_published_resource_id_after_discovery(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/catalogs") and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {"id": "catalog-bkn", "connector_type": "mysql"}
                        ]
                    },
                )
            if request.url.path.endswith("/catalogs/catalog-bkn"):
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "id": "catalog-bkn",
                                "name": "bkn",
                                "connector_type": "mysql",
                                "connector_config": {
                                    "database": "bkn",
                                    "host": "db.example.com",
                                    "port": 3306,
                                },
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/discover"):
                return httpx.Response(200, json={"id": "discover-1"})
            if request.url.path.endswith("/resources"):
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "id": "resource-1",
                                "name": "di_records_dataset123",
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        space = DataSpace(
            id="space-1",
            name="Default",
            slug="default",
            database_url_encrypted=encrypt_secret(
                "mysql+pymysql://reader:secret@db.example.com:3306/bkn"
            ),
            database_label="mysql://db.example.com:3306/bkn",
            is_default=True,
            status="ready",
        )
        result = await sync_space_catalog(
            space,
            access_token="access-token",
            gateway_url="http://gateway.test",
            transport=httpx.MockTransport(handler),
            resource_name="di_records_dataset123",
        )
        self.assertEqual(result["resource_id"], "resource-1")


if __name__ == "__main__":
    unittest.main()
