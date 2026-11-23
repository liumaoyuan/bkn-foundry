# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest
from pathlib import Path
from time import time_ns
from unittest.mock import patch

from sqlalchemy import func, select

from app.database import SessionLocal, init_database
from app.ingestion_service import create_ingestion_run
from app.models import Dataset, DatasetRecord, IngestionRun, PipelineTemplate
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
            self.assertTrue(all(stage.started_at for stage in first.stages))
            self.assertEqual(
                [stage.status for stage in first.stages],
                ["success"] * len(first.stages),
            )
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

    def test_user_pipeline_is_executed_and_updates_its_stages(self) -> None:
        db = SessionLocal()
        try:
            suffix = str(time_ns())
            dataset = Dataset(
                name=f"User pipeline test {suffix}",
                slug=f"user-pipeline-test-{suffix}",
                description="Automated user pipeline test",
                data_type="tabular",
                target_table="user_pipeline_test",
            )
            pipeline = PipelineTemplate(
                code=f"user-test-{suffix}",
                name="Test user pipeline",
                description="",
                supported_types=["csv"],
                steps=[
                    {
                        "key": "step-1",
                        "name": "Audit tool",
                        "kind": "mcp_tool",
                        "tool_name": "audit_tool",
                        "parameters": {"file_path": "${inputs.file_path}"},
                    }
                ],
            )
            db.add_all([dataset, pipeline])
            db.commit()
            db.refresh(dataset)
            run = create_ingestion_run(
                db,
                filename="pipeline.csv",
                content=b"name,value\nalpha,1\n",
                dataset_id=dataset.id,
                template_code=pipeline.code,
            )

            def execute(_code, _pipeline, *, on_step_start, on_step_result, **_kwargs):
                step = pipeline.steps[0]
                on_step_start(step, {"file_path": "/tmp/pipeline.csv"}, 1)
                result = {
                    "step": 1,
                    "key": "step-1",
                    "tool_name": "audit_tool",
                    "arguments": {"file_path": "/tmp/pipeline.csv"},
                    "result": {"status": "ok"},
                }
                on_step_result(result, 1)
                return [result]

            with patch("app.mcp_server._execute_user_pipeline", side_effect=execute):
                run_pipeline(run.id)
            db.expire_all()
            stored_run = db.get(IngestionRun, run.id)
            self.assertIsNotNone(stored_run)
            self.assertEqual(stored_run.status, "success")
            self.assertEqual([stage.status for stage in stored_run.stages], ["success"])
        finally:
            db.close()

    def test_appending_a_row_does_not_duplicate_existing_rows(self) -> None:
        db = SessionLocal()
        try:
            suffix = str(time_ns())
            dataset = Dataset(
                name=f"Append dedupe test {suffix}",
                slug=f"append-dedupe-test-{suffix}",
                description="",
                data_type="tabular",
                target_table="append_dedupe_test",
                storage_mode="shared",
            )
            db.add(dataset)
            db.commit()
            db.refresh(dataset)
            first = create_ingestion_run(
                db,
                filename="first.csv",
                content=b"id,name\n1,alpha\n2,beta\n",
                dataset_id=dataset.id,
                template_code="tabular_standard",
            )
            run_pipeline(first.id)
            second = create_ingestion_run(
                db,
                filename="second.csv",
                content=b"id,name\n1,alpha\n2,beta\n3,gamma\n",
                dataset_id=dataset.id,
                template_code="tabular_standard",
            )
            run_pipeline(second.id)
            db.expire_all()
            second = db.get(IngestionRun, second.id)
            self.assertIsNotNone(second)
            self.assertEqual(second.inserted_count, 1)
            self.assertEqual(second.duplicate_count, 2)
            total = db.scalar(
                select(func.count(DatasetRecord.id)).where(
                    DatasetRecord.dataset_id == dataset.id
                )
            )
            self.assertEqual(total, 3)
        finally:
            db.close()

    def test_duplicate_rows_inside_one_file_are_skipped_atomically(self) -> None:
        db = SessionLocal()
        try:
            suffix = str(time_ns())
            dataset = Dataset(
                name=f"Batch dedupe test {suffix}",
                slug=f"batch-dedupe-test-{suffix}",
                description="",
                data_type="tabular",
                target_table="batch_dedupe_test",
                storage_mode="shared",
            )
            db.add(dataset)
            db.commit()
            db.refresh(dataset)
            run = create_ingestion_run(
                db,
                filename="duplicates.csv",
                content=b"id,name\n1,alpha\n1,alpha\n2,beta\n",
                dataset_id=dataset.id,
                template_code="tabular_standard",
            )
            run_pipeline(run.id)
            db.expire_all()
            run = db.get(IngestionRun, run.id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, "success")
            self.assertEqual(run.inserted_count, 2)
            self.assertEqual(run.duplicate_count, 1)
        finally:
            db.close()

    def test_configured_business_key_updates_existing_shared_record(self) -> None:
        db = SessionLocal()
        try:
            suffix = str(time_ns())
            dataset = Dataset(
                name=f"Upsert test {suffix}",
                slug=f"upsert-test-{suffix}",
                description="",
                data_type="tabular",
                target_table="upsert_test",
                storage_mode="shared",
                upsert_keys=["id"],
            )
            db.add(dataset)
            db.commit()
            db.refresh(dataset)
            first = create_ingestion_run(
                db,
                filename="first.csv",
                content=b"id,name,value\n1,alpha,10\n",
                dataset_id=dataset.id,
                template_code="tabular_standard",
            )
            run_pipeline(first.id)
            second = create_ingestion_run(
                db,
                filename="second.csv",
                content=b"id,name,value\n1,alpha-updated,20\n",
                dataset_id=dataset.id,
                template_code="tabular_standard",
            )
            run_pipeline(second.id)
            db.expire_all()
            second = db.get(IngestionRun, second.id)
            self.assertIsNotNone(second)
            self.assertEqual(second.inserted_count, 0)
            self.assertEqual(second.updated_count, 1)
            self.assertEqual(second.duplicate_count, 0)
            records = db.scalars(
                select(DatasetRecord).where(DatasetRecord.dataset_id == dataset.id)
            ).all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].payload["name"], "alpha-updated")
            self.assertTrue(records[0].record_key)
        finally:
            db.close()
