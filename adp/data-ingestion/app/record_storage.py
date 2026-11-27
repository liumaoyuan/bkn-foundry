# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import and_, inspect, or_, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import DEDICATED_RECORD_THRESHOLD
from .models import Dataset, DatasetRecord, new_id


_TABLE_NAME = re.compile(r"^di_records_[a-zA-Z0-9_]+$")
_LOOKUP_CHUNK_SIZE = 500
_T = TypeVar("_T")


def business_record_key(keys: list[str], payload: dict[str, Any]) -> str:
    if not keys:
        return ""
    values: list[Any] = []
    for key in keys:
        if key not in payload or payload[key] is None or payload[key] == "":
            return ""
        values.append(payload[key])
    serialized = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def scope_record_key(record_key: str, scope: str) -> str:
    if not record_key or not scope:
        return record_key
    return hashlib.sha256(
        f"{scope}:{record_key}".encode("utf-8")
    ).hexdigest()


def _chunks(values: list[_T], size: int = _LOOKUP_CHUNK_SIZE) -> Iterator[list[_T]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def lock_dataset_for_write(db: Session, dataset_id: str) -> Dataset | None:
    """Serialize writes for one dataset without blocking unrelated datasets."""
    return db.scalar(
        select(Dataset)
        .where(Dataset.id == dataset_id)
        .with_for_update()
    )


def _existing_shared_fingerprints(
    db: Session,
    dataset_id: str,
    fingerprints: list[str],
) -> set[str]:
    existing: set[str] = set()
    for chunk in _chunks(fingerprints):
        existing.update(
            db.scalars(
                select(DatasetRecord.fingerprint).where(
                    DatasetRecord.dataset_id == dataset_id,
                    DatasetRecord.fingerprint.in_(chunk),
                )
            ).all()
        )
    return existing


def _insert_shared_ignore(
    db: Session,
    dataset_id: str,
    records: list[dict[str, Any]],
) -> None:
    table = DatasetRecord.__table__
    dialect = db.get_bind().dialect.name
    for chunk in _chunks(records, _LOOKUP_CHUNK_SIZE):
        rows = [{**record, "dataset_id": dataset_id} for record in chunk]
        if dialect == "sqlite":
            statement = sqlite_insert(table).values(rows).on_conflict_do_nothing(
                index_elements=["dataset_id", "fingerprint"]
            )
        elif dialect == "postgresql":
            statement = postgresql_insert(table).values(
                rows
            ).on_conflict_do_nothing(
                index_elements=["dataset_id", "fingerprint"]
            )
        elif dialect in {"mysql", "mariadb"}:
            # The dataset row lock prevents this path from racing. The no-op
            # conflict clause is a second guard for legacy callers.
            statement = mysql_insert(table).values(rows).on_duplicate_key_update(
                id=table.c.id
            )
        else:
            statement = table.insert().values(rows)
        db.execute(statement)


def persist_shared_records(
    db: Session,
    dataset_id: str,
    records: list[dict[str, Any]],
) -> tuple[int, int, int]:
    """Persist shared-table rows with bounded queries and atomic deduplication."""
    if not records:
        return 0, 0, 0

    if not any(str(record.get("record_key") or "") for record in records):
        unique_by_fingerprint: dict[str, dict[str, Any]] = {}
        for record in records:
            unique_by_fingerprint.setdefault(str(record["fingerprint"]), record)
        existing = _existing_shared_fingerprints(
            db,
            dataset_id,
            list(unique_by_fingerprint),
        )
        pending = [
            record
            for fingerprint, record in unique_by_fingerprint.items()
            if fingerprint not in existing
        ]
        if pending:
            _insert_shared_ignore(db, dataset_id, pending)
        return len(pending), 0, len(records) - len(pending)

    incoming_fingerprints = list(
        {str(record["fingerprint"]) for record in records}
    )
    existing_fingerprints = _existing_shared_fingerprints(
        db,
        dataset_id,
        incoming_fingerprints,
    )
    record_keys = sorted(
        {
            str(record["record_key"])
            for record in records
            if record.get("record_key")
        }
    )
    existing_by_key: dict[str, DatasetRecord] = {}
    for chunk in _chunks(record_keys):
        existing_by_key.update(
            {
                record.record_key: record
                for record in db.scalars(
                    select(DatasetRecord).where(
                        DatasetRecord.dataset_id == dataset_id,
                        DatasetRecord.record_key.in_(chunk),
                    )
                ).all()
            }
        )

    inserted = 0
    updated = 0
    duplicates = 0
    for item in records:
        record_key = str(item.get("record_key") or "")
        fingerprint = str(item["fingerprint"])
        existing = existing_by_key.get(record_key) if record_key else None
        if existing is not None:
            if existing.fingerprint == fingerprint:
                duplicates += 1
                continue
            existing_fingerprints.discard(existing.fingerprint)
            existing.run_id = str(item["run_id"])
            existing.fingerprint = fingerprint
            existing.data_type = str(item["data_type"])
            existing.payload = item["payload"]
            existing.asset_path = str(item.get("asset_path") or "")
            existing.source_id = (
                str(item["source_id"])
                if item.get("source_id") is not None
                else None
            )
            existing.source_object = str(item.get("source_object") or "")
            existing.source_locator = item.get("source_locator") or {}
            existing_fingerprints.add(fingerprint)
            updated += 1
            continue
        if fingerprint in existing_fingerprints:
            duplicates += 1
            continue
        new_record = DatasetRecord(
            id=str(item.get("id") or new_id()),
            dataset_id=dataset_id,
            run_id=str(item["run_id"]),
            fingerprint=fingerprint,
            record_key=record_key,
            data_type=str(item["data_type"]),
            payload=item["payload"],
            asset_path=str(item.get("asset_path") or ""),
            source_id=(
                str(item["source_id"])
                if item.get("source_id") is not None
                else None
            ),
            source_object=str(item.get("source_object") or ""),
            source_locator=item.get("source_locator") or {},
        )
        db.add(new_record)
        existing_fingerprints.add(fingerprint)
        if record_key:
            existing_by_key[record_key] = new_record
        inserted += 1
    return inserted, updated, duplicates


def dedicated_table_name(dataset: Dataset) -> str:
    return dataset.physical_table or f"di_records_{dataset.id}"


def _quote_table(name: str, engine: Engine) -> str:
    if not _TABLE_NAME.fullmatch(name):
        raise ValueError("invalid dedicated record table name")
    return engine.dialect.identifier_preparer.quote(name)


def _partition_name(value: datetime) -> str:
    return f"p{value:%Y%m}"


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return datetime(value.year + 1, 1, 1, tzinfo=UTC)
    return datetime(value.year, value.month + 1, 1, tzinfo=UTC)


def _utc_value(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _partition_clause(value: datetime) -> str:
    boundary = _next_month(value).date().isoformat()
    return (
        f"PARTITION {_partition_name(value)} VALUES LESS THAN "
        f"(TO_DAYS('{boundary}')), "
        "PARTITION pmax VALUES LESS THAN MAXVALUE"
    )


def ensure_dataset_table(
    engine: Engine,
    dataset: Dataset,
    *,
    at: datetime | None = None,
) -> str:
    table_name = dedicated_table_name(dataset)
    quoted = _quote_table(table_name, engine)
    dialect = engine.dialect.name
    payload_type = (
        "JSON" if dialect in {"mysql", "mariadb", "postgresql"} else "TEXT"
    )
    timestamp_type = "TIMESTAMP" if dialect == "postgresql" else "DATETIME(6)"
    partition_sql = ""
    if dialect in {"mysql", "mariadb"}:
        partition_sql = (
            " PARTITION BY RANGE (TO_DAYS(created_at)) ("
            + _partition_clause(at or datetime.now(UTC))
            + ")"
        )
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {quoted} ("
        "id VARCHAR(32) NOT NULL, "
        "run_id VARCHAR(32) NOT NULL, "
        "fingerprint VARCHAR(96) NOT NULL, "
        "record_key VARCHAR(96) NOT NULL DEFAULT '', "
        "data_type VARCHAR(32) NOT NULL, "
        "asset_path TEXT NOT NULL, "
        "source_id VARCHAR(32) NULL, "
        "source_object VARCHAR(255) NOT NULL DEFAULT '', "
        f"source_locator {payload_type} NULL, "
        "row_index BIGINT NOT NULL DEFAULT 0, "
        f"payload {payload_type} NOT NULL, "
        f"created_at {timestamp_type} NOT NULL, "
        "PRIMARY KEY (id, created_at), "
        "UNIQUE (run_id, fingerprint, created_at)"
        ")"
        + partition_sql
    )
    with engine.begin() as connection:
        connection.execute(text(ddl))
    existing = {
        column["name"]
        for column in inspect(engine).get_columns(table_name)
    }
    additions = {
        "record_key": "VARCHAR(96) NOT NULL DEFAULT ''",
        "source_id": "VARCHAR(32) NULL",
        "source_object": "VARCHAR(255) NOT NULL DEFAULT ''",
        "source_locator": f"{payload_type} NULL",
    }
    with engine.begin() as connection:
        for column, definition in additions.items():
            if column in existing:
                continue
            connection.execute(
                text(
                    f"ALTER TABLE {quoted} "
                    f"ADD COLUMN {column} {definition}"
                )
            )
    if dialect in {"mysql", "mariadb"}:
        ensure_month_partition(engine, table_name, at or datetime.now(UTC))
    return table_name


def ensure_month_partition(
    engine: Engine,
    table_name: str,
    value: datetime,
) -> None:
    if engine.dialect.name not in {"mysql", "mariadb"}:
        return
    quoted = _quote_table(table_name, engine)
    partition = _partition_name(value)
    with engine.begin() as connection:
        exists = connection.scalar(
            text(
                "SELECT partition_name FROM information_schema.PARTITIONS "
                "WHERE table_schema = DATABASE() "
                "AND table_name = :table_name "
                "AND partition_name = :partition_name"
            ),
            {"table_name": table_name, "partition_name": partition},
        )
        if exists:
            return
        has_pmax = connection.scalar(
            text(
                "SELECT partition_name FROM information_schema.PARTITIONS "
                "WHERE table_schema = DATABASE() "
                "AND table_name = :table_name "
                "AND partition_name = 'pmax'"
            ),
            {"table_name": table_name},
        )
        if not has_pmax:
            return
        connection.execute(
            text(
                f"ALTER TABLE {quoted} REORGANIZE PARTITION pmax INTO "
                f"({_partition_clause(value)})"
            )
        )


def materialize_dataset(
    db: Session,
    dataset: Dataset,
    *,
    at: datetime | None = None,
) -> str:
    engine = db.get_bind()
    table_name = ensure_dataset_table(engine, dataset, at=at)
    dataset.physical_table = table_name
    dataset.partition_mode = (
        "month" if engine.dialect.name in {"mysql", "mariadb"} else ""
    )
    dataset.storage_mode = "dedicated"
    shared = db.scalars(
        select(DatasetRecord)
        .where(DatasetRecord.dataset_id == dataset.id)
        .order_by(DatasetRecord.created_at.asc(), DatasetRecord.id.asc())
    ).all()
    if shared:
        insert_dedicated_records(
            engine,
            table_name,
            [
                {
                    "id": item.id,
                    "run_id": item.run_id,
                    "fingerprint": item.fingerprint,
                    "record_key": item.record_key,
                    "data_type": item.data_type,
                    "asset_path": item.asset_path,
                    "source_id": item.source_id,
                    "source_object": item.source_object,
                    "source_locator": item.source_locator or {},
                    "row_index": index,
                    "payload": item.payload,
                    "created_at": item.created_at,
                }
                for index, item in enumerate(shared)
            ],
        )
        db.query(DatasetRecord).filter(
            DatasetRecord.dataset_id == dataset.id
        ).delete(synchronize_session=False)
    return table_name


def prepare_dataset_storage(
    db: Session,
    dataset: Dataset,
    *,
    incoming_count: int,
    at: datetime | None = None,
) -> str:
    if dataset.storage_mode == "dedicated":
        table_name = ensure_dataset_table(db.get_bind(), dataset, at=at)
        dataset.physical_table = table_name
        dataset.partition_mode = (
            "month"
            if db.get_bind().dialect.name in {"mysql", "mariadb"}
            else ""
        )
        return table_name
    if (
        dataset.storage_mode == "auto"
        and dataset.record_count + incoming_count >= DEDICATED_RECORD_THRESHOLD
    ):
        return materialize_dataset(db, dataset, at=at)
    return ""


def insert_dedicated_records(
    engine: Engine,
    table_name: str,
    records: list[dict[str, Any]],
) -> int:
    if not records:
        return 0
    ensure_month_partition(engine, table_name, records[0]["created_at"])
    quoted = _quote_table(table_name, engine)
    unique_records: list[dict[str, Any]] = []
    batch_fingerprints: set[str] = set()
    for record in records:
        fingerprint = str(record["fingerprint"])
        if fingerprint in batch_fingerprints:
            continue
        batch_fingerprints.add(fingerprint)
        unique_records.append(record)
    fingerprints = list(batch_fingerprints)
    with engine.begin() as connection:
        existing: set[str] = set()
        for offset in range(0, len(fingerprints), 500):
            chunk = fingerprints[offset : offset + 500]
            placeholders = ", ".join(
                f":fingerprint_{index}" for index in range(len(chunk))
            )
            existing.update(
                connection.scalars(
                    text(
                        f"SELECT fingerprint FROM {quoted} "
                        f"WHERE fingerprint IN ({placeholders})"
                    ),
                    {
                        f"fingerprint_{index}": fingerprint
                        for index, fingerprint in enumerate(chunk)
                    },
                ).all()
            )
        unique_records = [
            record
            for record in unique_records
            if str(record["fingerprint"]) not in existing
        ]
        if not unique_records:
            return 0
        rows = [
            {
                **record,
                "record_key": record.get("record_key", ""),
                "source_id": record.get("source_id"),
                "source_object": record.get("source_object", ""),
                "source_locator": json.dumps(
                    record.get("source_locator") or {},
                    ensure_ascii=False,
                    default=str,
                ),
                "payload": json.dumps(record["payload"], ensure_ascii=False, default=str),
            }
            for record in unique_records
        ]
        dialect = engine.dialect.name
        insert_sql = (
            "INSERT OR IGNORE"
            if dialect == "sqlite"
            else "INSERT"
            if dialect == "postgresql"
            else "INSERT IGNORE"
        )
        conflict_sql = (
            " ON CONFLICT DO NOTHING" if dialect == "postgresql" else ""
        )
        source_locator_bind = (
            "CAST(:source_locator AS JSON)"
            if dialect == "postgresql"
            else ":source_locator"
        )
        payload_bind = (
            "CAST(:payload AS JSON)"
            if dialect == "postgresql"
            else ":payload"
        )
        connection.execute(
            text(
                f"{insert_sql} INTO {quoted} "
                "(id, run_id, fingerprint, record_key, data_type, asset_path, "
                "source_id, source_object, source_locator, row_index, payload, created_at) "
                "VALUES (:id, :run_id, :fingerprint, :record_key, :data_type, :asset_path, "
                f":source_id, :source_object, {source_locator_bind}, :row_index, "
                f"{payload_bind}, :created_at){conflict_sql}"
            ),
            rows,
        )
    return len(unique_records)


def upsert_dedicated_records(
    engine: Engine,
    table_name: str,
    records: list[dict[str, Any]],
) -> tuple[int, int, int]:
    if not records:
        return 0, 0, 0
    ensure_month_partition(engine, table_name, records[0]["created_at"])
    quoted = _quote_table(table_name, engine)
    inserted = 0
    updated = 0
    duplicates = 0
    with engine.begin() as connection:
        record_keys = sorted(
            {
                str(record.get("record_key") or "")
                for record in records
                if record.get("record_key")
            }
        )
        existing_by_key: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(record_keys):
            placeholders = ", ".join(
                f":record_key_{index}" for index in range(len(chunk))
            )
            rows = connection.execute(
                text(
                    f"SELECT id, fingerprint, record_key FROM {quoted} "
                    f"WHERE record_key IN ({placeholders})"
                ),
                {
                    f"record_key_{index}": record_key
                    for index, record_key in enumerate(chunk)
                },
            ).mappings()
            existing_by_key.update(
                {
                    str(row["record_key"]): dict(row)
                    for row in rows
                }
            )

        fingerprints = sorted(
            {str(record["fingerprint"]) for record in records}
        )
        existing_fingerprints: set[str] = set()
        for chunk in _chunks(fingerprints):
            placeholders = ", ".join(
                f":fingerprint_{index}" for index in range(len(chunk))
            )
            existing_fingerprints.update(
                connection.scalars(
                    text(
                        f"SELECT fingerprint FROM {quoted} "
                        f"WHERE fingerprint IN ({placeholders})"
                    ),
                    {
                        f"fingerprint_{index}": fingerprint
                        for index, fingerprint in enumerate(chunk)
                    },
                ).all()
            )

        pending: list[dict[str, Any]] = []
        update_rows: list[dict[str, Any]] = []
        for record in records:
            record_key = str(record.get("record_key") or "")
            fingerprint = str(record["fingerprint"])
            existing_row = existing_by_key.get(record_key) if record_key else None
            if existing_row:
                if existing_row["fingerprint"] == fingerprint:
                    duplicates += 1
                    continue
                existing_id = str(existing_row["id"])
                existing_fingerprints.discard(str(existing_row["fingerprint"]))
                existing_fingerprints.add(fingerprint)
                existing_row.update(
                    {
                        "run_id": record["run_id"],
                        "fingerprint": fingerprint,
                        "data_type": record["data_type"],
                        "asset_path": record.get("asset_path", ""),
                        "source_id": record.get("source_id"),
                        "source_object": record.get("source_object", ""),
                        "source_locator": json.dumps(
                            record.get("source_locator") or {},
                            ensure_ascii=False,
                            default=str,
                        ),
                        "row_index": record["row_index"],
                        "payload": record["payload"],
                    }
                )
                update_rows.append(
                    {
                        **record,
                        "source_id": record.get("source_id"),
                        "source_object": record.get("source_object", ""),
                        "source_locator": json.dumps(
                            record.get("source_locator") or {},
                            ensure_ascii=False,
                            default=str,
                        ),
                        "payload": json.dumps(
                            record["payload"],
                            ensure_ascii=False,
                            default=str,
                        ),
                        "id": existing_id,
                    }
                )
                updated += 1
                continue
            if fingerprint in existing_fingerprints:
                duplicates += 1
                continue
            pending.append(record)
            existing_fingerprints.add(fingerprint)
            if record_key:
                existing_by_key[record_key] = {
                    "id": record["id"],
                    "fingerprint": fingerprint,
                    "record_key": record_key,
                }
            inserted += 1

        if update_rows:
            dialect = engine.dialect.name
            source_locator_bind = (
                "CAST(:source_locator AS JSON)"
                if dialect == "postgresql"
                else ":source_locator"
            )
            payload_bind = (
                "CAST(:payload AS JSON)"
                if dialect == "postgresql"
                else ":payload"
            )
            connection.execute(
                text(
                    f"UPDATE {quoted} SET run_id = :run_id, "
                    "fingerprint = :fingerprint, data_type = :data_type, "
                    "asset_path = :asset_path, source_id = :source_id, "
                    "source_object = :source_object, "
                    f"source_locator = {source_locator_bind}, "
                    f"row_index = :row_index, payload = {payload_bind} "
                    "WHERE id = :id"
                ),
                update_rows,
            )
        if pending:
            dialect = engine.dialect.name
            insert_sql = (
                "INSERT OR IGNORE"
                if dialect == "sqlite"
                else "INSERT"
                if dialect == "postgresql"
                else "INSERT IGNORE"
            )
            conflict_sql = (
                " ON CONFLICT DO NOTHING" if dialect == "postgresql" else ""
            )
            source_locator_bind = (
                "CAST(:source_locator AS JSON)"
                if dialect == "postgresql"
                else ":source_locator"
            )
            payload_bind = (
                "CAST(:payload AS JSON)"
                if dialect == "postgresql"
                else ":payload"
            )
            connection.execute(
                text(
                    f"{insert_sql} INTO {quoted} "
                    "(id, run_id, fingerprint, record_key, data_type, asset_path, "
                    "source_id, source_object, source_locator, "
                    "row_index, payload, created_at) VALUES "
                    "(:id, :run_id, :fingerprint, :record_key, :data_type, "
                    f":asset_path, :source_id, :source_object, {source_locator_bind}, "
                    f":row_index, {payload_bind}, :created_at){conflict_sql}"
                ),
                [
                    {
                        **record,
                        "source_id": record.get("source_id"),
                        "source_object": record.get("source_object", ""),
                        "source_locator": json.dumps(
                            record.get("source_locator") or {},
                            ensure_ascii=False,
                            default=str,
                        ),
                        "payload": json.dumps(
                            record["payload"],
                            ensure_ascii=False,
                            default=str,
                        ),
                    }
                    for record in pending
                ],
            )
    return inserted, updated, duplicates


def backfill_dedicated_record_keys(
    engine: Engine,
    table_name: str,
    keys: list[str],
    *,
    scoped_source_ids: set[str] | None = None,
) -> int:
    if not keys:
        return 0
    quoted = _quote_table(table_name, engine)
    changed = 0
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                f"SELECT id, source_id, source_object, payload FROM {quoted}"
            )
        ).mappings().all()
        for row in rows:
            payload = (
                json.loads(row["payload"])
                if isinstance(row["payload"], str)
                else row["payload"]
            )
            record_key = business_record_key(keys, payload)
            if not record_key:
                continue
            source_id = str(row.get("source_id") or "")
            if scoped_source_ids and source_id in scoped_source_ids:
                record_key = scope_record_key(
                    record_key,
                    str(row.get("source_object") or ""),
                )
            connection.execute(
                text(
                    f"UPDATE {quoted} SET record_key = :record_key "
                    "WHERE id = :id"
                ),
                {"id": row["id"], "record_key": record_key},
            )
            changed += 1
    return changed


def count_dedicated_records(engine: Engine, table_name: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(
                    f"SELECT COUNT(*) FROM {_quote_table(table_name, engine)}"
                )
            )
            or 0
        )


def sample_dedicated_records(
    engine: Engine,
    table_name: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT id, run_id, fingerprint, record_key, data_type, asset_path, "
                f"source_id, source_object, source_locator, payload, created_at "
                f"FROM {_quote_table(table_name, engine)} "
                "ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            {"limit": limit},
        ).mappings()
        return [
            {
                **dict(row),
                "payload": json.loads(row["payload"])
                if isinstance(row["payload"], str)
                else row["payload"],
                "source_locator": json.loads(row["source_locator"])
                if isinstance(row["source_locator"], str)
                else row["source_locator"] or {},
                "created_at": _utc_value(row["created_at"]),
            }
            for row in rows
        ]


def iter_dedicated_payloads(
    engine: Engine,
    table_name: str,
    *,
    batch_size: int = 1000,
):
    with engine.connect() as connection:
        result = connection.execution_options(
            stream_results=True
        ).execute(
            text(
                f"SELECT payload FROM {_quote_table(table_name, engine)} "
                "ORDER BY created_at ASC, id ASC"
            )
        )
        while True:
            batch = result.mappings().fetchmany(batch_size)
            if not batch:
                break
            for row in batch:
                yield (
                    json.loads(row["payload"])
                    if isinstance(row["payload"], str)
                    else row["payload"]
                )


def backfill_dedicated_provenance(
    engine: Engine,
    table_name: str,
    run_metadata: dict[str, dict[str, Any]],
) -> int:
    quoted = _quote_table(table_name, engine)
    changed = 0
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                f"SELECT id, run_id, source_object, source_locator "
                f"FROM {quoted} ORDER BY created_at ASC, id ASC"
            )
        ).mappings().all()
        for index, row in enumerate(rows, start=1):
            existing_locator = (
                json.loads(row["source_locator"])
                if isinstance(row["source_locator"], str)
                else row["source_locator"]
            )
            if row["source_object"] and existing_locator:
                continue
            metadata = run_metadata.get(str(row["run_id"]), {})
            source_object = str(
                row["source_object"]
                or metadata.get("source_object")
                or metadata.get("original_name")
                or ""
            )
            locator = existing_locator or {
                "kind": "legacy_backfill",
                "file": source_object,
                "row_number": index,
                "source_uri": metadata.get("source_path") or "",
            }
            connection.execute(
                text(
                    f"UPDATE {quoted} SET source_id = :source_id, "
                    "source_object = :source_object, "
                    "source_locator = :source_locator WHERE id = :id"
                ),
                {
                    "id": row["id"],
                    "source_id": metadata.get("source_id"),
                    "source_object": source_object,
                    "source_locator": json.dumps(
                        locator,
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            )
            changed += 1
    return changed


def page_dedicated_records(
    engine: Engine,
    table_name: str,
    *,
    page: int,
    page_size: int,
    query: str,
) -> tuple[int, list[dict[str, Any]]]:
    where = ""
    params: dict[str, Any] = {}
    if query.strip():
        payload_text_type = (
            "TEXT" if engine.dialect.name == "postgresql" else "CHAR"
        )
        where = (
            f" WHERE LOWER(CAST(payload AS {payload_text_type})) LIKE :query "
        )
        params["query"] = f"%{query.strip().lower()}%"
    quoted = _quote_table(table_name, engine)
    with engine.connect() as connection:
        total = int(
            connection.scalar(
                text(f"SELECT COUNT(*) FROM {quoted}{where}"),
                params,
            )
            or 0
        )
        rows = connection.execute(
            text(
                f"SELECT id, run_id, fingerprint, record_key, data_type, asset_path, "
                f"source_id, source_object, source_locator, payload, created_at "
                f"FROM {quoted}{where} "
                "ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
            ),
            {
                **params,
                "limit": page_size,
                "offset": max(page - 1, 0) * page_size,
            },
        ).mappings()
        items = [
            {
                **dict(row),
                "payload": json.loads(row["payload"])
                if isinstance(row["payload"], str)
                else row["payload"],
                "source_locator": json.loads(row["source_locator"])
                if isinstance(row["source_locator"], str)
                else row["source_locator"] or {},
                "created_at": _utc_value(row["created_at"]),
            }
            for row in rows
        ]
    return total, items


def delete_dedicated_run(engine: Engine, table_name: str, run_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                f"DELETE FROM {_quote_table(table_name, engine)} "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )


def delete_dedicated_source_records(
    engine: Engine,
    table_name: str,
    *,
    source_id: str,
    source_object: str,
    keep_fingerprints: set[str] | None = None,
) -> None:
    with engine.begin() as connection:
        quoted = _quote_table(table_name, engine)
        rows = connection.execute(
            text(
                f"SELECT id, fingerprint FROM {quoted} "
                "WHERE source_id = :source_id "
                "AND source_object = :source_object"
            ),
            {
                "source_id": source_id,
                "source_object": source_object,
            },
        ).mappings()
        stale_ids = [
            str(row["id"])
            for row in rows
            if keep_fingerprints is None
            or str(row["fingerprint"]) not in keep_fingerprints
        ]
        for chunk in _chunks(stale_ids):
            placeholders = ", ".join(
                f":record_id_{index}" for index in range(len(chunk))
            )
            connection.execute(
                text(f"DELETE FROM {quoted} WHERE id IN ({placeholders})"),
                {
                    f"record_id_{index}": record_id
                    for index, record_id in enumerate(chunk)
                },
            )


def prune_dedicated_source_records(
    engine: Engine,
    table_name: str,
    *,
    source_id: str,
    active_source_objects: set[str],
) -> None:
    with engine.begin() as connection:
        if not active_source_objects:
            connection.execute(
                text(
                    f"DELETE FROM {_quote_table(table_name, engine)} "
                    "WHERE source_id = :source_id"
                ),
                {"source_id": source_id},
            )
            return
        placeholders = ", ".join(
            f":source_object_{index}"
            for index in range(len(active_source_objects))
        )
        connection.execute(
            text(
                f"DELETE FROM {_quote_table(table_name, engine)} "
                "WHERE source_id = :source_id "
                f"AND source_object NOT IN ({placeholders})"
            ),
            {
                "source_id": source_id,
                **{
                    f"source_object_{index}": value
                    for index, value in enumerate(
                        sorted(active_source_objects)
                    )
                },
            },
        )


def drop_dedicated_table(engine: Engine, table_name: str) -> None:
    if not table_name:
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                f"DROP TABLE IF EXISTS {_quote_table(table_name, engine)}"
            )
        )
