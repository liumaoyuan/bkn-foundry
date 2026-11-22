# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.database import SessionLocal, init_database
from app.downstream_contract import (
    decode_record_cursor,
    encode_record_cursor,
    infer_dataset_schema,
)
from app.main import dataset_record_changes, dataset_schema
from app.models import Dataset, DatasetRecord, IngestionRun


class DownstreamContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()

    def test_schema_inference_reports_types_and_nullability(self) -> None:
        records = [
            SimpleNamespace(
                payload={"name": "alpha", "count": 1, "ratio": 0.5},
            ),
            SimpleNamespace(
                payload={"name": "beta", "count": 2, "ratio": None},
            ),
            SimpleNamespace(
                payload={"name": None, "ratio": 1.5, "active": True},
            ),
        ]

        schema = infer_dataset_schema(records, total_records=12)

        self.assertEqual(schema["schema_version"], "inferred-v1")
        self.assertEqual(schema["sampled_records"], 3)
        self.assertEqual(schema["total_records"], 12)
        fields = {field["name"]: field for field in schema["fields"]}
        self.assertEqual(fields["name"]["data_type"], "string")
        self.assertTrue(fields["name"]["nullable"])
        self.assertEqual(fields["count"]["data_type"], "integer")
        self.assertTrue(fields["count"]["nullable"])
        self.assertEqual(fields["ratio"]["data_type"], "number")
        self.assertTrue(fields["ratio"]["nullable"])
        self.assertEqual(fields["active"]["data_type"], "boolean")
        self.assertTrue(fields["active"]["nullable"])

    def test_record_cursor_is_dataset_scoped_and_round_trips(self) -> None:
        created_at = datetime.now(UTC)
        record = SimpleNamespace(
            dataset_id="dataset-a",
            created_at=created_at,
            id="record-a",
        )

        cursor = encode_record_cursor(record)
        decoded = decode_record_cursor(cursor, "dataset-a")

        self.assertEqual(decoded.record_id, "record-a")
        self.assertEqual(decoded.created_at, created_at)
        with self.assertRaisesRegex(ValueError, "different dataset"):
            decode_record_cursor(cursor, "dataset-b")

    def test_changes_endpoint_pages_in_stable_order(self) -> None:
        suffix = uuid4().hex[:10]
        db = SessionLocal()
        dataset = Dataset(
            name=f"Downstream contract {suffix}",
            slug=f"downstream-contract-{suffix}",
            description="Downstream contract test",
            data_type="tabular",
            target_table="downstream_contract",
        )
        db.add(dataset)
        db.flush()
        run = IngestionRun(
            dataset_id=dataset.id,
            template_code="tabular_standard",
            status="success",
            original_name="contract.csv",
        )
        db.add(run)
        db.flush()
        base_time = datetime.now(UTC) - timedelta(minutes=1)
        for index in range(3):
            db.add(
                DatasetRecord(
                    dataset_id=dataset.id,
                    run_id=run.id,
                    fingerprint=f"contract-{suffix}-{index}",
                    data_type="tabular",
                    payload={"name": f"row-{index}", "value": index},
                    created_at=base_time + timedelta(seconds=index),
                )
            )
        db.commit()
        try:
            schema = dataset_schema(dataset.id, sample_size=10, db=db)
            self.assertEqual(schema["total_records"], 3)
            self.assertEqual(
                [field["name"] for field in schema["fields"]],
                ["name", "value"],
            )

            collected: list[str] = []
            cursor: str | None = None
            terminal_cursor: str | None = None
            while True:
                page = dataset_record_changes(
                    dataset.id,
                    cursor=cursor,
                    limit=1,
                    db=db,
                )
                collected.extend(item["id"] for item in page["items"])
                next_cursor = page["next_cursor"]
                if not page["has_more"]:
                    terminal_cursor = next_cursor
                    break
                cursor = next_cursor

            self.assertEqual(len(collected), 3)
            self.assertEqual(len(set(collected)), 3)
            repeated = dataset_record_changes(
                dataset.id,
                cursor=terminal_cursor,
                limit=1,
                db=db,
            )
            self.assertEqual(repeated["items"], [])
            self.assertFalse(repeated["has_more"])
            self.assertEqual(repeated["next_cursor"], terminal_cursor)
        finally:
            for record in db.query(DatasetRecord).filter(
                DatasetRecord.dataset_id == dataset.id
            ):
                db.delete(record)
            db.delete(run)
            db.delete(dataset)
            db.commit()
            db.close()


if __name__ == "__main__":
    unittest.main()
