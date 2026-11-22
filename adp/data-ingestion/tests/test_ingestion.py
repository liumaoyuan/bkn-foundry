# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest
from pathlib import Path
from time import time_ns

from sqlalchemy import func, select

from app.database import SessionLocal, init_database
from app.ingestion_service import create_ingestion_run
from app.models import Dataset, DatasetRecord, IngestionRun
from app.pipeline import run_pipeline
from app.seed import seed_database
from app.storage import storage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IngestionServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()
        storage.ensure_ready()
        db = SessionLocal()
        try:
            seed_database(db)
        finally:
            db.close()

    def test_csv_ingestion_is_idempotent(self) -> None:
        fixture = PROJECT_ROOT / "taiwan_marine_observations.csv"
        content = fixture.read_bytes()
        db = SessionLocal()
        try:
            suffix = str(time_ns())
            dataset = Dataset(
                name=f"Ingestion test {suffix}",
                slug=f"ingestion-test-{suffix}",
                description="Automated ingestion test",
                data_type="tabular",
                target_table="ingestion_test",
            )
            db.add(dataset)
            db.commit()
            db.refresh(dataset)

            first = create_ingestion_run(
                db,
                filename=fixture.name,
                content=content,
                dataset_id=dataset.id,
                template_code="tabular_standard",
            )
            run_pipeline(first.id)
            db.expire_all()
            first = db.get(IngestionRun, first.id)
            self.assertIsNotNone(first)
            self.assertEqual(first.status, "success")
            self.assertGreater(first.inserted_count, 0)
            first_inserted = first.inserted_count

            second = create_ingestion_run(
                db,
                filename=fixture.name,
                content=content,
                dataset_id=dataset.id,
                template_code="tabular_standard",
            )
            run_pipeline(second.id)
            db.expire_all()
            second = db.get(IngestionRun, second.id)
            self.assertIsNotNone(second)
            self.assertEqual(second.status, "success")
            self.assertEqual(second.inserted_count, 0)
            self.assertEqual(second.duplicate_count, first_inserted)
            total = db.scalar(
                select(func.count(DatasetRecord.id)).where(
                    DatasetRecord.dataset_id == dataset.id
                )
            )
            self.assertEqual(total, first_inserted)
        finally:
            db.close()
