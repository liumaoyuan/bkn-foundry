# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import DatasetRecord


SCHEMA_VERSION = "inferred-v1"
CURSOR_VERSION = 1


@dataclass(frozen=True)
class RecordCursor:
    dataset_id: str
    created_at: datetime
    record_id: str


def _scalar_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "mixed"


def _summarize_types(types: set[str]) -> str:
    non_null = types - {"null"}
    if not non_null:
        return "null"
    if non_null == {"integer", "number"}:
        return "number"
    if len(non_null) == 1:
        return next(iter(non_null))
    return "mixed"


def infer_dataset_schema(
    records: Iterable[DatasetRecord],
    *,
    total_records: int,
) -> dict[str, Any]:
    """Infer an advisory field contract from a bounded record sample."""
    sampled = list(records)
    fields: dict[str, dict[str, Any]] = {}
    for record in sampled:
        payload = record.payload if isinstance(record.payload, dict) else {}
        for name, value in payload.items():
            field = fields.setdefault(
                str(name),
                {"types": set(), "present_count": 0},
            )
            field["present_count"] += 1
            field["types"].add(_scalar_type(value))

    return {
        "schema_version": SCHEMA_VERSION,
        "sampled_records": len(sampled),
        "total_records": total_records,
        "fields": [
            {
                "name": name,
                "data_type": _summarize_types(field["types"]),
                "nullable": (
                    field["present_count"] < len(sampled)
                    or "null" in field["types"]
                ),
            }
            for name, field in fields.items()
        ],
    }


def encode_record_cursor(record: DatasetRecord) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "dataset_id": record.dataset_id,
        "created_at": record.created_at.isoformat(),
        "id": record.id,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def decode_record_cursor(cursor: str, dataset_id: str) -> RecordCursor:
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )
    except (
        binascii.Error,
        UnicodeDecodeError,
        TypeError,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("invalid dataset record cursor") from error
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ValueError("unsupported cursor version")
    encoded_dataset_id = str(payload.get("dataset_id") or "")
    if encoded_dataset_id != dataset_id:
        raise ValueError("cursor belongs to a different dataset")
    record_id = str(payload.get("id") or "")
    try:
        created_at = datetime.fromisoformat(str(payload.get("created_at") or ""))
    except ValueError as error:
        raise ValueError("invalid dataset record cursor timestamp") from error
    if not record_id or created_at.tzinfo is None:
        raise ValueError("cursor is incomplete")
    return RecordCursor(
        dataset_id=encoded_dataset_id,
        created_at=created_at,
        record_id=record_id,
    )
