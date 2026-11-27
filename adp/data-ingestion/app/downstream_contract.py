# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import base64
import binascii
import hashlib
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


def _accumulate_fields(
    records: Iterable[DatasetRecord],
) -> tuple[int, dict[str, dict[str, Any]]]:
    sampled_count = 0
    fields: dict[str, dict[str, Any]] = {}
    for record in records:
        sampled_count += 1
        payload = record.payload if isinstance(record.payload, dict) else {}
        for name, value in payload.items():
            if str(name).startswith("__"):
                continue
            field = fields.setdefault(
                str(name),
                {
                    "types": set(),
                    "present_count": 0,
                    "sample_values": [],
                },
            )
            field["types"].add(_scalar_type(value))
            if value is not None and not (
                isinstance(value, str) and value.strip() == ""
            ):
                field["present_count"] += 1
            if (
                value is not None
                and len(field["sample_values"]) < 3
                and all(value != item for item in field["sample_values"])
            ):
                field["sample_values"].append(_sample_value(value))
    return sampled_count, fields


def _serialize_schema(
    fields: dict[str, dict[str, Any]],
    *,
    sampled_count: int,
    total_records: int,
    primary_key_fields: list[str] | None = None,
) -> dict[str, Any]:
    primary_keys = list(primary_key_fields or [])
    serialized_fields = [
        {
            "name": name,
            "data_type": _summarize_types(field["types"]),
            "nullable": (
                field["present_count"] < sampled_count
                or "null" in field["types"]
            ),
            "present_count": field["present_count"],
            "completeness": round(
                field["present_count"] / sampled_count, 4
            )
            if sampled_count
            else 0,
            "sample_values": field["sample_values"],
            "primary_key": name in primary_keys,
        }
        for name, field in fields.items()
    ]
    schema_digest = hashlib.sha256(
        json.dumps(
            serialized_fields,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "schema_version": f"{SCHEMA_VERSION}:{schema_digest}",
        "sampled_records": sampled_count,
        "total_records": total_records,
        "primary_key_fields": primary_keys,
        "stable_key_strategy": (
            "business_key" if primary_keys else "content_fingerprint"
        ),
        "fields": serialized_fields,
    }


def infer_dataset_schema(
    records: Iterable[DatasetRecord],
    *,
    total_records: int,
    primary_key_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Infer an advisory field contract from a bounded record sample."""
    sampled_count, fields = _accumulate_fields(records)
    return _serialize_schema(
        fields,
        sampled_count=sampled_count,
        total_records=total_records,
        primary_key_fields=primary_key_fields,
    )


def build_quality_report(
    records: Iterable[DatasetRecord],
    *,
    total_records: int,
    primary_key_fields: list[str] | None = None,
    duplicate_count: int = 0,
    error_count: int = 0,
    scope: str = "sample",
) -> dict[str, Any]:
    primary_keys = list(primary_key_fields or [])
    sampled_count, fields = _accumulate_fields(records)
    schema = _serialize_schema(
        fields,
        sampled_count=sampled_count,
        total_records=total_records,
        primary_key_fields=primary_keys,
    )
    issues: list[dict[str, Any]] = []
    completeness_total = 0.0
    for field in schema["fields"]:
        completeness_total += float(field["completeness"])
        if field["completeness"] < 1:
            issues.append(
                {
                    "code": "field_missing",
                    "severity": (
                        "error" if field["primary_key"] else "warning"
                    ),
                    "field": field["name"],
                    "message": (
                        f"字段 {field['name']} 在当前样本中完整率为 "
                        f"{field['completeness'] * 100:.1f}%"
                    ),
                }
            )
        if field["data_type"] == "mixed":
            issues.append(
                {
                    "code": "field_mixed_types",
                    "severity": "warning",
                    "field": field["name"],
                    "message": f"字段 {field['name']} 包含多种数据类型",
                }
            )
    field_count = len(schema["fields"])
    field_names = {field["name"] for field in schema["fields"]}
    for primary_key in primary_keys:
        if primary_key not in field_names:
            issues.append(
                {
                    "code": "primary_key_missing",
                    "severity": "error",
                    "field": primary_key,
                    "message": f"样本中不存在业务主键字段：{primary_key}",
                }
            )
    completeness = (
        completeness_total / field_count if field_count else 1.0
    )
    if duplicate_count:
        issues.append(
            {
                "code": "duplicate_records",
                "severity": "warning",
                "message": f"最近一次入库跳过 {duplicate_count} 条重复记录",
            }
        )
    if error_count:
        issues.append(
            {
                "code": "ingestion_errors",
                "severity": "error",
                "message": f"最近一次入库存在 {error_count} 条错误记录",
            }
        )
    has_error = any(item["severity"] == "error" for item in issues)
    score = max(
        0,
        min(
            100,
            round(
                completeness * 100
                - (10 if has_error else 0)
                - min(duplicate_count / max(total_records, 1) * 20, 10)
            ),
        ),
    )
    return {
        "status": "failed" if has_error else "passed",
        "score": score,
        "scope": scope,
        "sampled_records": sampled_count,
        "total_records": total_records,
        "field_count": field_count,
        "completeness": round(completeness, 4),
        "duplicate_count": duplicate_count,
        "error_count": error_count,
        "primary_key_fields": primary_keys,
        "issues": issues,
    }


def stable_record_id(
    dataset_id: str,
    fingerprint: str,
    record_key: str = "",
) -> str:
    identity = record_key or fingerprint
    return hashlib.sha256(
        f"{dataset_id}:{identity}".encode("utf-8")
    ).hexdigest()[:32]


def _sample_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > 120:
            return f"{value[:117]}..."
        return value
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = str(value)
    return serialized[:120] + ("..." if len(serialized) > 120 else "")


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
