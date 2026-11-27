# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import BackgroundTasks
from starlette.requests import Request

from app.auto_scan import AutoScanScheduler
from app.auto_scan_api import run_job_now as run_scan_job_now
from app.database import SessionLocal, init_database
from app.models import AutoScanJob, DataSource, Dataset, DatasetRecord
from app.record_storage import (
    count_dedicated_records,
    drop_dedicated_table,
    sample_dedicated_records,
)
from app.seed import seed_database


class AutoScanSnapshotConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()
        db = SessionLocal()
        try:
            seed_database(db)
        finally:
            db.close()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = SessionLocal()
        self.suffix = uuid4().hex[:8]
        self.dataset = Dataset(
            name=f"Snapshot target {self.suffix}",
            slug=f"snapshot-target-{self.suffix}",
            data_type="tabular",
            target_table=f"snapshot_{self.suffix}",
        )
        self.db.add(self.dataset)
        self.db.flush()

    def tearDown(self) -> None:
        if self.dataset.physical_table:
            drop_dedicated_table(
                self.db.get_bind(),
                self.dataset.physical_table,
            )
        self.db.query(DatasetRecord).filter(
            DatasetRecord.dataset_id == self.dataset.id
        ).delete(synchronize_session=False)
        self.db.query(AutoScanJob).filter(
            AutoScanJob.dataset_id == self.dataset.id
        ).delete(synchronize_session=False)
        self.db.query(DataSource).filter(
            DataSource.name.like(f"%{self.suffix}%")
        ).delete(synchronize_session=False)
        self.db.query(Dataset).filter(Dataset.id == self.dataset.id).delete(
            synchronize_session=False
        )
        self.db.commit()
        self.db.close()
        self.temporary.cleanup()

    def _records(self) -> list[DatasetRecord]:
        self.db.expire_all()
        return list(
            self.db.query(DatasetRecord)
            .filter(DatasetRecord.dataset_id == self.dataset.id)
            .order_by(DatasetRecord.created_at, DatasetRecord.id)
            .all()
        )

    def test_local_file_changes_replace_old_snapshot_and_deleted_file_is_pruned(
        self,
    ) -> None:
        root = Path(self.temporary.name)
        source_file = root / "items.csv"
        source_file.write_text("id,value\n1,old\n2,keep\n", encoding="utf-8")
        source = DataSource(
            name=f"Local snapshot {self.suffix}",
            source_type="local_dir",
            config={"path": str(root)},
        )
        self.db.add(source)
        self.db.flush()
        job = AutoScanJob(
            name=f"Local job {self.suffix}",
            source_id=source.id,
            dataset_id=self.dataset.id,
            template_code="auto",
            scan_type="local",
            path=str(root),
            file_pattern="*",
            enabled=True,
        )
        self.db.add(job)
        self.db.commit()
        scheduler = AutoScanScheduler()

        with patch("app.auto_scan.MCP_ALLOWED_ROOTS", [str(root)]):
            scheduler._scan_local(self.db, job, source, "default")
            first = self._records()
            self.assertEqual(len(first), 2)

            source_file.write_text(
                "id,value\n1,updated\n3,new\n",
                encoding="utf-8",
            )
            scheduler._scan_local(self.db, job, source, "default")
            second = self._records()
            self.assertEqual(len(second), 2)
            self.assertEqual(
                {row.payload["value"] for row in second},
                {"updated", "new"},
            )

            source_file.unlink()
            scheduler._scan_local(self.db, job, source, "default")
            self.db.commit()

        self.assertEqual(self._records(), [])
        self.assertEqual(self.dataset.record_count, 0)

    def test_later_success_does_not_clear_an_earlier_file_failure(self) -> None:
        root = Path(self.temporary.name)
        broken = root / "broken.csv"
        valid = root / "valid.csv"
        broken.write_text("id\n1\n", encoding="utf-8")
        valid.write_text("id,required\n2,ok\n", encoding="utf-8")
        self.dataset.upsert_keys = ["required"]
        source = DataSource(
            name=f"Partial failure source {self.suffix}",
            source_type="local_dir",
            config={"path": str(root)},
        )
        self.db.add(source)
        self.db.flush()
        job = AutoScanJob(
            name=f"Partial failure job {self.suffix}",
            source_id=source.id,
            dataset_id=self.dataset.id,
            template_code="auto",
            scan_type="local",
            path=str(root),
            file_pattern="*",
            enabled=True,
        )
        self.db.add(job)
        self.db.commit()
        scheduler = AutoScanScheduler()

        with (
            patch("app.auto_scan.MCP_ALLOWED_ROOTS", [str(root)]),
            patch.object(Path, "glob", return_value=iter([broken, valid])),
        ):
            scheduler._scan_local(self.db, job, source, "default")

        self.assertIn("业务主键", job.last_error)
        self.assertEqual(
            {row.payload["required"] for row in self._records()},
            {"ok"},
        )

    def test_manual_folder_ingestion_drains_all_files(self) -> None:
        root = Path(self.temporary.name)
        for index in range(5):
            (root / f"item-{index}.csv").write_text(
                f"id,value\n{index},{index}\n",
                encoding="utf-8",
            )
        source = DataSource(
            name=f"Folder all files {self.suffix}",
            source_type="local_dir",
            config={"path": str(root)},
        )
        self.db.add(source)
        self.db.flush()
        job = AutoScanJob(
            name=f"Folder all files job {self.suffix}",
            source_id=source.id,
            dataset_id=self.dataset.id,
            template_code="auto",
            scan_type="local",
            path=str(root),
            file_pattern="*.csv",
            enabled=True,
        )
        self.db.add(job)
        self.db.commit()
        scheduler = AutoScanScheduler()

        with (
            patch("app.auto_scan.MCP_ALLOWED_ROOTS", [str(root)]),
            patch("app.auto_scan.AUTO_SCAN_MAX_FILES_PER_RUN", 2),
        ):
            asyncio.run(
                scheduler.run_job_now(
                    job.id,
                    "default",
                    drain=True,
                )
            )

        self.db.expire_all()
        self.assertEqual(
            self.db.query(DatasetRecord)
            .filter(DatasetRecord.dataset_id == self.dataset.id)
            .count(),
            5,
        )

    def test_manual_scan_returns_queued_without_waiting_for_completion(self) -> None:
        source = DataSource(
            name=f"Async scan source {self.suffix}",
            source_type="local_dir",
            config={"path": self.temporary.name},
        )
        self.db.add(source)
        self.db.flush()
        job = AutoScanJob(
            name=f"Async scan job {self.suffix}",
            source_id=source.id,
            dataset_id=self.dataset.id,
            template_code="auto",
            scan_type="local",
            path=self.temporary.name,
            enabled=True,
        )
        self.db.add(job)
        self.db.commit()
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/api/auto-scan/jobs/{job.id}/run",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
            }
        )
        request.state.data_space_id = "default"
        background_tasks = BackgroundTasks()

        result = asyncio.run(
            run_scan_job_now(
                job_id=job.id,
                request=request,
                background_tasks=background_tasks,
                drain=False,
                wait=False,
                _space_db=self.db,
            )
        )

        self.db.refresh(job)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(job.status, "queued")
        self.assertEqual(len(background_tasks.tasks), 1)

    def test_database_full_snapshot_removes_stale_rows(self) -> None:
        source_path = Path(self.temporary.name) / "source.db"
        source_url = f"sqlite+pysqlite:///{source_path.as_posix()}"
        connection = sqlite3.connect(source_path)
        try:
            connection.execute(
                "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.executemany(
                "INSERT INTO items (id, value) VALUES (?, ?)",
                [(1, "old"), (2, "keep")],
            )
            connection.commit()
        finally:
            connection.close()

        source = DataSource(
            name=f"Database snapshot {self.suffix}",
            source_type="mysql",
            config={
                "query_mode": "table",
                "table_name": "items",
            },
        )
        self.db.add(source)
        self.db.flush()
        job = AutoScanJob(
            name=f"Database job {self.suffix}",
            source_id=source.id,
            dataset_id=self.dataset.id,
            template_code="auto",
            scan_type="database",
            enabled=True,
        )
        self.db.add(job)
        self.db.commit()
        scheduler = AutoScanScheduler()

        with patch("app.auto_scan.database_url", return_value=source_url):
            scheduler._scan_database(self.db, job, source, "default")
            self.assertEqual(len(self._records()), 2)

            connection = sqlite3.connect(source_path)
            try:
                connection.execute(
                    "INSERT INTO items (id, value) VALUES (3, 'new')"
                )
                connection.commit()
            finally:
                connection.close()
            scheduler._scan_database(self.db, job, source, "default")
            self.assertEqual(
                {row.payload["value"] for row in self._records()},
                {"old", "keep", "new"},
            )

            connection = sqlite3.connect(source_path)
            try:
                connection.execute("DELETE FROM items WHERE id = 1")
                connection.execute(
                    "UPDATE items SET value = 'updated' WHERE id = 2"
                )
                connection.commit()
            finally:
                connection.close()

            scheduler._scan_database(self.db, job, source, "default")

        rows = self._records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row.payload["value"] for row in rows},
            {"updated", "new"},
        )

    def test_database_snapshot_ignores_unchanged_row_order(self) -> None:
        source_path = Path(self.temporary.name) / "unordered-source.db"
        source_url = f"sqlite+pysqlite:///{source_path.as_posix()}"
        connection = sqlite3.connect(source_path)
        try:
            connection.execute(
                "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.executemany(
                "INSERT INTO items (id, value) VALUES (?, ?)",
                [(1, "a"), (2, "b")],
            )
            connection.commit()
        finally:
            connection.close()
        source = DataSource(
            name=f"Unordered snapshot {self.suffix}",
            source_type="mysql",
            config={
                "query_mode": "table",
                "table_names": ["items"],
            },
        )
        self.db.add(source)
        self.db.flush()
        job = AutoScanJob(
            name=f"Unordered job {self.suffix}",
            source_id=source.id,
            dataset_id=self.dataset.id,
            template_code="auto",
            scan_type="database",
            enabled=True,
        )
        self.db.add(job)
        self.db.commit()
        scheduler = AutoScanScheduler()

        with patch("app.auto_scan.database_url", return_value=source_url):
            scheduler._scan_database(self.db, job, source, "default")
            first_run_ids = {row.run_id for row in self._records()}

            connection = sqlite3.connect(source_path)
            try:
                connection.execute("DELETE FROM items")
                connection.executemany(
                    "INSERT INTO items (id, value) VALUES (?, ?)",
                    [(2, "b"), (1, "a")],
                )
                connection.commit()
            finally:
                connection.close()

            scheduler._scan_database(self.db, job, source, "default")

        self.assertEqual({row.run_id for row in self._records()}, first_run_ids)
        self.assertEqual(job.processed_count, 1)

    def test_database_snapshot_prunes_dedicated_record_table(self) -> None:
        source_path = Path(self.temporary.name) / "dedicated-source.db"
        source_url = f"sqlite+pysqlite:///{source_path.as_posix()}"
        connection = sqlite3.connect(source_path)
        try:
            connection.execute(
                "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.executemany(
                "INSERT INTO items (id, value) VALUES (?, ?)",
                [(1, "old"), (2, "keep")],
            )
            connection.commit()
        finally:
            connection.close()
        self.dataset.storage_mode = "dedicated"
        source = DataSource(
            name=f"Dedicated database snapshot {self.suffix}",
            source_type="mysql",
            config={
                "query_mode": "table",
                "table_name": "items",
            },
        )
        self.db.add(source)
        self.db.flush()
        job = AutoScanJob(
            name=f"Dedicated database job {self.suffix}",
            source_id=source.id,
            dataset_id=self.dataset.id,
            template_code="auto",
            scan_type="database",
            enabled=True,
        )
        self.db.add(job)
        self.db.commit()
        scheduler = AutoScanScheduler()

        with patch("app.auto_scan.database_url", return_value=source_url):
            scheduler._scan_database(self.db, job, source, "default")
            self.db.refresh(self.dataset)
            self.assertEqual(
                count_dedicated_records(
                    self.db.get_bind(),
                    self.dataset.physical_table,
                ),
                2,
            )

            connection = sqlite3.connect(source_path)
            try:
                connection.execute(
                    "INSERT INTO items (id, value) VALUES (3, 'new')"
                )
                connection.commit()
            finally:
                connection.close()
            scheduler._scan_database(self.db, job, source, "default")
            self.assertEqual(
                {
                    row["payload"]["value"]
                    for row in sample_dedicated_records(
                        self.db.get_bind(),
                        self.dataset.physical_table,
                        limit=10,
                    )
                },
                {"old", "keep", "new"},
            )

            connection = sqlite3.connect(source_path)
            try:
                connection.execute("DELETE FROM items WHERE id = 1")
                connection.execute(
                    "UPDATE items SET value = 'updated' WHERE id = 2"
                )
                connection.commit()
            finally:
                connection.close()
            scheduler._scan_database(self.db, job, source, "default")

        rows = sample_dedicated_records(
            self.db.get_bind(),
            self.dataset.physical_table,
            limit=10,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["payload"]["value"] for row in rows},
            {"updated", "new"},
        )

    def test_single_table_snapshot_is_idempotent_and_updates_by_primary_key(self) -> None:
        source_path = Path(self.temporary.name) / "single-table-source.db"
        source_url = f"sqlite+pysqlite:///{source_path.as_posix()}"
        connection = sqlite3.connect(source_path)
        try:
            connection.execute(
                "CREATE TABLE customers (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.executemany(
                "INSERT INTO customers (id, value) VALUES (?, ?)",
                [(1, "first"), (2, "second")],
            )
            connection.commit()
        finally:
            connection.close()
        source = DataSource(
            name=f"Single table source {self.suffix}",
            source_type="mysql",
            config={
                "query_mode": "table",
                "table_name": "customers",
            },
        )
        self.db.add(source)
        self.db.flush()
        job = AutoScanJob(
            name=f"Single table job {self.suffix}",
            source_id=source.id,
            dataset_id=self.dataset.id,
            template_code="auto",
            scan_type="database",
            enabled=True,
        )
        self.db.add(job)
        self.db.commit()
        scheduler = AutoScanScheduler()

        with patch("app.auto_scan.database_url", return_value=source_url):
            scheduler._scan_database(self.db, job, source, "default")
            self.db.refresh(self.dataset)
            self.assertEqual(self.dataset.upsert_keys, ["id"])
            first = self._records()
            self.assertEqual(len(first), 2)
            first_run_ids = {row.run_id for row in first}
            first_processed = job.processed_count

            scheduler._scan_database(self.db, job, source, "default")
            self.assertEqual(job.processed_count, first_processed)
            self.assertEqual({row.run_id for row in self._records()}, first_run_ids)

            repeat_job = AutoScanJob(
                name=f"Repeated single table job {self.suffix}",
                source_id=source.id,
                dataset_id=self.dataset.id,
                template_code="auto",
                scan_type="database",
                enabled=True,
            )
            self.db.add(repeat_job)
            self.db.commit()
            scheduler._scan_database(self.db, repeat_job, source, "default")
            self.assertIsNone(repeat_job.last_run_id)
            self.assertEqual(repeat_job.processed_count, 0)
            self.assertEqual({row.run_id for row in self._records()}, first_run_ids)

            connection = sqlite3.connect(source_path)
            try:
                connection.execute(
                    "UPDATE customers SET value = 'updated' WHERE id = 1"
                )
                connection.commit()
            finally:
                connection.close()

            scheduler._scan_database(self.db, job, source, "default")
            updated = self._records()
            self.assertEqual(len(updated), 2)
            self.assertEqual(
                next(
                    row.payload["value"]
                    for row in updated
                    if row.source_object == "customers"
                        and str(row.payload["id"]) == "1"
                ),
                "updated",
            )
            self.assertEqual({row.source_object for row in updated}, {"customers"})
            self.assertEqual(len({row.record_key for row in updated}), 2)
            updated_run_id = job.last_run_id

            connection = sqlite3.connect(source_path)
            try:
                connection.execute(
                    "UPDATE customers SET value = 'first' WHERE id = 1"
                )
                connection.commit()
            finally:
                connection.close()

            scheduler._scan_database(self.db, job, source, "default")
            self.assertNotEqual(job.last_run_id, updated_run_id)
            self.assertEqual(
                next(
                    row.payload["value"]
                    for row in self._records()
                    if row.source_object == "customers"
                    and str(row.payload["id"]) == "1"
                ),
                "first",
            )


if __name__ == "__main__":
    unittest.main()
