#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import asyncio
import io
import hashlib
import stat
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.auto_scan import AutoScanScheduler
from app.models import AutoScanJob, DataSource


class FakeEntry:
    def __init__(self, filename: str, directory: bool = False) -> None:
        self.filename = filename
        self.st_mode = stat.S_IFDIR if directory else stat.S_IFREG


class FakeSftpClient:
    def __init__(self) -> None:
        self.tree = {
            "/remote": [FakeEntry(f"file-{index:02d}.csv") for index in range(21)],
        }

    def listdir_attr(self, path: str):
        return self.tree.get(path, [])

    def open(self, path: str, _mode: str):
        return io.BytesIO(path.encode("utf-8"))

    def close(self) -> None:
        return None


class FakeDb:
    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


class RemoteAutoScanTest(unittest.TestCase):
    def test_sftp_skips_seen_files_without_hiding_later_files(self) -> None:
        scheduler = AutoScanScheduler()
        source = DataSource(
            name="SFTP",
            source_type="sftp",
            config={"host": "sftp.test", "path": "/remote"},
        )
        job = AutoScanJob(
            name="remote",
            source_id="source-1",
            dataset_id="dataset-1",
            template_code="auto",
            scan_type="sftp",
            path="/remote",
        )
        client = FakeSftpClient()
        for index in range(20):
            path = f"/remote/file-{index:02d}.csv"
            scheduler._mark_seen(
                job,
                f"sftp:sftp.test:{path}",
                hashlib.sha256(path.encode("utf-8")).hexdigest(),
            )

        with (
            patch("paramiko.Transport") as transport,
            patch("paramiko.SFTPClient.from_transport", return_value=client),
            patch.object(
                scheduler,
                "_ingest",
                return_value=SimpleNamespace(status="success"),
            ) as ingest,
            patch.object(scheduler, "_prune_missing_source_records") as prune,
        ):
            scheduler._scan_remote(FakeDb(), job, source, "default")

        transport.return_value.connect.assert_called_once()
        self.assertEqual(ingest.call_count, 1)
        self.assertEqual(ingest.call_args.kwargs["filename"], "file-20.csv")
        prune.assert_called_once()

    def test_sftp_recursive_scan_enters_child_directories(self) -> None:
        scheduler = AutoScanScheduler()
        source = DataSource(
            name="SFTP",
            source_type="sftp",
            config={"host": "sftp.test", "path": "/root"},
        )
        job = AutoScanJob(
            name="remote",
            source_id="source-1",
            dataset_id="dataset-1",
            template_code="auto",
            scan_type="sftp",
            path="/root",
            recursive=True,
        )

        class RecursiveClient(FakeSftpClient):
            def __init__(self) -> None:
                self.tree = {
                    "/root": [FakeEntry("child", directory=True)],
                    "/root/child": [FakeEntry("nested.csv")],
                }

        with (
            patch("paramiko.Transport"),
            patch(
                "paramiko.SFTPClient.from_transport",
                return_value=RecursiveClient(),
            ),
            patch.object(
                scheduler,
                "_ingest",
                return_value=SimpleNamespace(status="success"),
            ) as ingest,
            patch.object(scheduler, "_prune_missing_source_records"),
        ):
            scheduler._scan_remote(FakeDb(), job, source, "default")

        self.assertEqual(ingest.call_count, 1)
        self.assertEqual(ingest.call_args.kwargs["filename"], "nested.csv")

    def test_disabled_job_does_not_stay_running(self) -> None:
        scheduler = AutoScanScheduler()
        job = AutoScanJob(
            name="disabled",
            source_id="source-1",
            dataset_id="dataset-1",
            template_code="auto",
            scan_type="api",
            enabled=False,
            status="running",
        )
        db = FakeDb()
        db.get = lambda *_args: job
        with patch("app.auto_scan.session_for_space", return_value=db):
            scheduler._scan_job_sync("job-1", "default")
        self.assertEqual(job.status, "ready")

    def test_scan_result_error_is_not_cleared_by_job_status(self) -> None:
        scheduler = AutoScanScheduler()
        source = DataSource(
            name="Local",
            source_type="local_dir",
            config={"path": "/data"},
        )
        job = AutoScanJob(
            name="local",
            source_id="source-1",
            dataset_id="dataset-1",
            template_code="auto",
            scan_type="local",
            status="running",
            enabled=True,
        )

        class JobDb(FakeDb):
            def get(self, model, _identifier):
                return job if model is AutoScanJob else source

            def rollback(self) -> None:
                return None

        def fail_scan(_db, current_job, _source, _space_id):
            current_job.last_error = "入库失败：测试错误"

        with (
            patch("app.auto_scan.session_for_space", return_value=JobDb()),
            patch.object(scheduler, "_scan_local", side_effect=fail_scan),
        ):
            scheduler._scan_job_sync("job-1", "default")

        self.assertEqual(job.status, "ready")
        self.assertEqual(job.last_error, "入库失败：测试错误")

    def test_cancel_job_signals_worker_and_waits_for_completion(self) -> None:
        scheduler = AutoScanScheduler()
        cancel_event = threading.Event()
        done_event = threading.Event()
        done_event.set()
        scheduler._job_events["job-1"] = (cancel_event, done_event)
        self.assertTrue(asyncio.run(scheduler.cancel_job("job-1", timeout=1)))
        self.assertTrue(cancel_event.is_set())


if __name__ == "__main__":
    unittest.main()
