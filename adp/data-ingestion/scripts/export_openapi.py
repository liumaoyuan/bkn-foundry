# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml


SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]
OUTPUT = REPOSITORY_ROOT / "docs" / "api" / "data-ingestion" / "openapi.yaml"

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DATA_INGESTION_AUTH_ENABLED", "false")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("AUTO_SCAN_ENABLED", "false")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="openbkn-openapi-"))

from app.main import app  # noqa: E402


def public_spec() -> dict:
    spec = app.openapi()
    paths: dict = {}
    for path, operation in spec.get("paths", {}).items():
        if path.startswith("/api/"):
            path = f"/api/data-ingestion/{path.removeprefix('/api/')}"
        paths[path] = operation
    spec["paths"] = paths
    spec["servers"] = [{"url": "/", "description": "OpenBKN gateway"}]
    components = spec.setdefault("components", {})
    components.setdefault("securitySchemes", {})["OAuth2"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "OAuth 2.0 access token or AppKey",
    }
    components.setdefault("parameters", {})["DataIngestionSpace"] = {
        "name": "X-Data-Ingestion-Space",
        "in": "header",
        "required": False,
        "description": (
            "Data space ID or slug. Defaults to 'default' for backward compatibility."
        ),
        "schema": {"type": "string", "default": "default"},
    }
    for path, path_item in spec["paths"].items():
        if path.startswith("/api/data-ingestion/spaces"):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            parameters = operation.setdefault("parameters", [])
            parameters.append({"$ref": "#/components/parameters/DataIngestionSpace"})
    spec["security"] = [{"OAuth2": []}]
    return spec


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        yaml.safe_dump(
            public_spec(),
            allow_unicode=True,
            sort_keys=False,
            width=100,
        ),
        encoding="utf-8",
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
