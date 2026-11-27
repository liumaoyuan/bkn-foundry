# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import DEDICATED_RECORD_THRESHOLD
from app.models import Base, Dataset, DatasetRecord
from app.record_storage import (
    backfill_dedicated_record_keys,
    count_dedicated_records,
    drop_dedicated_table,
    ensure_dataset_table,
    insert_dedicated_records,
    page_dedicated_records,
    prepare_dataset_storage,
    upsert_dedicated_records,
)


class DedicatedRecordStorageTest(unittest.TestCase):
    def test_dedicated_table_stores_and_pages_records(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        dataset = Dataset(
            id="dataset123456789",
            name="Large dataset",
            slug="large-dataset",
        )
        table_name = ensure_dataset_table(engine, dataset)
        created_at = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
        insert_dedicated_records(
            engine,
            table_name,
            [
                {
                    "id": "record-1",
                    "run_id": "run-1",
                    "fingerprint": "fp-1",
                    "data_type": "row",
                    "asset_path": "",
                    "row_index": 0,
                    "payload": {"station": "A1", "value": 22.4},
                    "created_at": created_at,
                },
                {
                    "id": "record-2",
                    "run_id": "run-1",
                    "fingerprint": "fp-2",
                    "data_type": "row",
                    "asset_path": "",
                    "row_index": 1,
                    "payload": {"station": "A2", "value": 23.1},
                    "created_at": created_at,
                },
            ],
        )
        self.assertEqual(count_dedicated_records(engine, table_name), 2)
        inserted_again = insert_dedicated_records(
            engine,
            table_name,
            [
                {
                    "id": "record-3",
                    "run_id": "run-2",
                    "fingerprint": "fp-1",
                    "data_type": "row",
                    "asset_path": "",
                    "row_index": 0,
                    "payload": {"station": "A1", "value": 22.4},
                    "created_at": created_at,
                }
            ],
        )
        self.assertEqual(inserted_again, 0)
        self.assertEqual(count_dedicated_records(engine, table_name), 2)
        total, items = page_dedicated_records(
            engine,
            table_name,
            page=1,
            page_size=10,
            query="A1",
        )
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["payload"]["station"], "A1")
        drop_dedicated_table(engine, table_name)

    def test_auto_mode_materializes_shared_records_at_threshold(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            dataset = Dataset(
                id="autodataset12345",
                name="Auto dataset",
                slug="auto-dataset",
                storage_mode="auto",
                record_count=DEDICATED_RECORD_THRESHOLD,
            )
            db.add(dataset)
            db.add(
                DatasetRecord(
                    id="shared-record",
                    dataset_id=dataset.id,
                    run_id="run-1",
                    fingerprint="fp-1",
                    data_type="row",
                    payload={"value": 1},
                )
            )
            db.commit()
            table_name = prepare_dataset_storage(
                db,
                dataset,
                incoming_count=1,
                at=datetime(2026, 11, 23, 10, 0, tzinfo=UTC),
            )
            db.commit()
            self.assertEqual(dataset.storage_mode, "dedicated")
            self.assertEqual(dataset.physical_table, table_name)
            self.assertEqual(count_dedicated_records(engine, table_name), 1)
            self.assertEqual(
                db.query(DatasetRecord).filter(
                    DatasetRecord.dataset_id == dataset.id
                ).count(),
                0,
            )

    def test_dedicated_upsert_updates_record_by_business_key(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        dataset = Dataset(
            id="upsertdataset1234",
            name="Upsert dataset",
            slug="upsert-dataset",
            storage_mode="dedicated",
            upsert_keys=["id"],
        )
        table_name = ensure_dataset_table(engine, dataset)
        created_at = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
        base = {
            "id": "record-1",
            "run_id": "run-1",
            "fingerprint": "fp-1",
            "record_key": "key-1",
            "data_type": "row",
            "asset_path": "",
            "row_index": 0,
            "payload": {"id": 1, "value": "old"},
            "created_at": created_at,
        }
        self.assertEqual(upsert_dedicated_records(engine, table_name, [base]), (1, 0, 0))
        changed = {
            **base,
            "id": "record-2",
            "run_id": "run-2",
            "fingerprint": "fp-2",
            "payload": {"id": 1, "value": "new"},
        }
        self.assertEqual(
            upsert_dedicated_records(engine, table_name, [changed]),
            (0, 1, 0),
        )
        self.assertEqual(count_dedicated_records(engine, table_name), 1)
        _, items = page_dedicated_records(
            engine,
            table_name,
            page=1,
            page_size=10,
            query="",
        )
        self.assertEqual(items[0]["payload"]["value"], "new")

    def test_dedicated_business_keys_can_be_backfilled(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        dataset = Dataset(
            id="backfilldataset1",
            name="Backfill dataset",
            slug="backfill-dataset",
        )
        table_name = ensure_dataset_table(engine, dataset)
        created_at = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
        insert_dedicated_records(
            engine,
            table_name,
            [
                {
                    "id": "record-1",
                    "run_id": "run-1",
                    "fingerprint": "fp-1",
                    "data_type": "row",
                    "asset_path": "",
                    "source_id": "source-db",
                    "source_object": "customers",
                    "row_index": 0,
                    "payload": {"id": 1, "value": "old"},
                    "created_at": created_at,
                },
                {
                    "id": "record-2",
                    "run_id": "run-1",
                    "fingerprint": "fp-2",
                    "data_type": "row",
                    "asset_path": "",
                    "source_id": "source-db",
                    "source_object": "orders",
                    "row_index": 0,
                    "payload": {"id": 1, "value": "old"},
                    "created_at": created_at,
                },
            ],
        )
        self.assertEqual(
            backfill_dedicated_record_keys(
                engine,
                table_name,
                ["id"],
                scoped_source_ids={"source-db"},
            ),
            2,
        )
        _, items = page_dedicated_records(
            engine,
            table_name,
            page=1,
            page_size=10,
            query="",
        )
        self.assertEqual(len({item["record_key"] for item in items}), 2)


if __name__ == "__main__":
    unittest.main()
