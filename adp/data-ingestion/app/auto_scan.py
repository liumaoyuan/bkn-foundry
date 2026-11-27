from __future__ import annotations

import asyncio
import csv
import fnmatch
import hashlib
import io
import json
import logging
import posixpath
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from ftplib import FTP, FTP_TLS
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import create_engine, func, inspect as sqlalchemy_inspect, or_, select, text

from .config import (
    AUTO_SCAN_ENABLED,
    AUTO_SCAN_MAX_FILES_PER_RUN,
    AUTO_SCAN_TICK_SECONDS,
    MCP_ALLOWED_ROOTS,
)
from .database import (
    DEFAULT_SPACE_ID,
    list_data_spaces,
    session_for_space,
)
from .data_source_service import resolved_source_config
from .file_inspector import inspect_file
from .ingestion_service import (
    content_fingerprint,
    create_ingestion_run,
    find_duplicate_ingestion_run,
)
from .models import (
    AutoScanJob,
    DataSource,
    Dataset,
    DatasetRecord,
    IngestionRun,
    utc_now,
)
from .pipeline import run_pipeline
from .record_storage import (
    count_dedicated_records,
    delete_dedicated_source_records,
    prune_dedicated_source_records,
)
from .source_connectors import (
    configured_table_names,
    database_source_query,
    database_url,
    load_sftp_private_key,
    request_api_source,
)


logger = logging.getLogger(__name__)
SUPPORTED_SUFFIXES = {
    ".csv",
    ".xlsx",
    ".xlsm",
    ".pdf",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def next_scan_at(job: AutoScanJob, base: datetime | None = None) -> datetime:
    try:
        timezone = ZoneInfo(job.timezone or "Asia/Shanghai")
    except Exception as error:
        raise ValueError(f"无效时区：{job.timezone}") from error
    now = base or datetime.now(UTC)
    local_now = now.astimezone(timezone)
    if job.schedule_type == "interval":
        return now + timedelta(seconds=max(30, job.interval_seconds))
    if job.schedule_type in {"daily", "weekly"}:
        try:
            hour, minute = (int(part) for part in job.schedule_time.split(":", 1))
        except (TypeError, ValueError) as error:
            raise ValueError("执行时间必须为 HH:MM") from error
        candidates = []
        weekdays = set(job.weekdays or [])
        for offset in range(0, 8):
            day = local_now + timedelta(days=offset)
            if job.schedule_type == "weekly" and day.weekday() not in weekdays:
                continue
            candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > local_now:
                candidates.append(candidate)
        if not candidates:
            raise ValueError("没有可用的下一次执行时间")
        return min(candidates).astimezone(UTC)
    if job.schedule_type == "cron":
        if not job.cron_expression.strip():
            raise ValueError("Cron 模式需要填写表达式")
        try:
            next_local = croniter(job.cron_expression, local_now).get_next(datetime)
        except Exception as error:
            raise ValueError(f"Cron 表达式无效：{job.cron_expression}") from error
        return next_local.astimezone(UTC)
    raise ValueError(f"不支持的调度类型：{job.schedule_type}")


class AutoScanScheduler:
    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self._job_events: dict[str, tuple[threading.Event, threading.Event]] = {}
        self._cancel_requested: set[str] = set()
        self._event_lock = threading.Lock()

    async def start(self) -> None:
        if not AUTO_SCAN_ENABLED or self.task:
            return
        self._reset_interrupted_jobs()
        self.task = asyncio.create_task(self._loop(), name="auto-scan-scheduler")

    @staticmethod
    def _reset_interrupted_jobs() -> None:
        for space in list_data_spaces():
            db = session_for_space(space.id)
            try:
                db.query(AutoScanJob).filter(
                    AutoScanJob.status == "running"
                ).update(
                    {
                        AutoScanJob.status: "error",
                        AutoScanJob.last_error: "服务重启导致扫描中断",
                        AutoScanJob.updated_at: utc_now(),
                    },
                    synchronize_session=False,
                )
                db.commit()
            finally:
                db.close()

    async def stop(self) -> None:
        if not self.task:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        finally:
            self.task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.scan_due_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic scan cycle failed")
            await asyncio.sleep(AUTO_SCAN_TICK_SECONDS)

    async def scan_due_jobs(self) -> int:
        job_refs: list[tuple[str, str]] = []
        for space in list_data_spaces():
            try:
                job_refs.extend(
                    (space.id, job_id)
                    for job_id in self._claim_due_jobs(space.id)
                )
            except Exception:
                logger.exception("Unable to scan data space %s", space.id)
        for space_id, job_id in job_refs:
            await asyncio.to_thread(self._scan_job_sync, job_id, space_id)
        return len(job_refs)

    def _claim_due_jobs(self, space_id: str) -> list[str]:
        db = session_for_space(space_id)
        try:
            now = utc_now()
            jobs = db.scalars(
                select(AutoScanJob)
                .where(
                    AutoScanJob.enabled.is_(True),
                    AutoScanJob.status != "running",
                    or_(
                        AutoScanJob.next_scan_at.is_(None),
                        AutoScanJob.next_scan_at <= now,
                    ),
                )
                .order_by(AutoScanJob.next_scan_at.asc())
                .limit(5)
            ).all()
            job_ids = [job.id for job in jobs]
            for job in jobs:
                job.status = "running"
                job.last_scan_at = now
                job.next_scan_at = next_scan_at(job, now)
            db.commit()
        finally:
            db.close()
        return job_ids

    async def run_job_now(
        self,
        job_id: str,
        space_id: str = DEFAULT_SPACE_ID,
        *,
        drain: bool = False,
    ) -> None:
        previous_processed = -1
        previous_run_id: str | None = None
        # Manual "ingest this source" drains one 20-file batch at a time.
        # The limit prevents a source that changes on every read from looping forever.
        max_passes = 100 if drain else 1
        for _ in range(max_passes):
            db = session_for_space(space_id)
            try:
                job = db.get(AutoScanJob, job_id)
                if job is None:
                    raise LookupError("扫描任务不存在")
                if job.status == "running":
                    raise RuntimeError("扫描任务正在运行")
                job.status = "running"
                job.last_scan_at = utc_now()
                job.next_scan_at = next_scan_at(job, utc_now())
                db.commit()
            finally:
                db.close()
            await asyncio.to_thread(self._scan_job_sync, job_id, space_id)
            if not drain:
                break
            db = session_for_space(space_id)
            try:
                job = db.get(AutoScanJob, job_id)
                if job is None:
                    raise LookupError("扫描任务不存在")
                if job.last_error:
                    break
                if (
                    job.processed_count == previous_processed
                    and job.last_run_id == previous_run_id
                ):
                    break
                previous_processed = job.processed_count
                previous_run_id = job.last_run_id
            finally:
                db.close()

    async def cancel_job(self, job_id: str, timeout: float = 30.0) -> bool:
        with self._event_lock:
            self._cancel_requested.add(job_id)
            events = self._job_events.get(job_id)
            if events:
                events[0].set()
        if not events:
            return True
        finished = await asyncio.to_thread(events[1].wait, timeout)
        with self._event_lock:
            self._cancel_requested.discard(job_id)
        return bool(finished)

    def _is_cancelled(self, job_id: str) -> bool:
        with self._event_lock:
            events = self._job_events.get(job_id)
            return job_id in self._cancel_requested or bool(
                events and events[0].is_set()
            )

    def _scan_job_sync(
        self,
        job_id: str,
        space_id: str = DEFAULT_SPACE_ID,
    ) -> None:
        cancel_event = threading.Event()
        done_event = threading.Event()
        with self._event_lock:
            self._job_events[job_id] = (cancel_event, done_event)
            if job_id in self._cancel_requested:
                cancel_event.set()
        db = session_for_space(space_id)
        try:
            job = db.get(AutoScanJob, job_id)
            if job is None:
                return
            if self._is_cancelled(job_id):
                job.status = "ready"
                job.updated_at = utc_now()
                db.commit()
                return
            if not job.enabled:
                job.status = "ready"
                job.updated_at = utc_now()
                db.commit()
                return
            job.last_error = ""
            source = db.get(DataSource, job.source_id)
            if source is None:
                raise RuntimeError("扫描任务关联的数据源不存在")
            if job.scan_type == "local":
                self._scan_local(db, job, source, space_id)
            elif job.scan_type in {"ftp", "sftp"}:
                self._scan_remote(db, job, source, space_id)
            elif job.scan_type == "database":
                self._scan_database(db, job, source, space_id)
            elif job.scan_type == "api":
                self._scan_api(db, job, source, space_id)
            else:
                raise ValueError(f"不支持的扫描类型：{job.scan_type}")
            job.status = "ready"
            job.updated_at = utc_now()
            db.commit()
        except Exception as error:
            logger.exception("Automatic scan job failed: %s", job_id)
            db.rollback()
            job = db.get(AutoScanJob, job_id)
            if job:
                job.status = "error"
                job.last_error = f"{type(error).__name__}: {error}"
                job.updated_at = utc_now()
                db.commit()
        finally:
            db.close()
            with self._event_lock:
                self._job_events.pop(job_id, None)
                self._cancel_requested.discard(job_id)
            done_event.set()

    def _scan_local(
        self,
        db,
        job: AutoScanJob,
        source: DataSource,
        space_id: str,
    ) -> None:
        config = resolved_source_config(source)
        raw_path = Path(job.path or config.get("path", "")).expanduser()
        if not raw_path.is_absolute():
            raw_path = Path(MCP_ALLOWED_ROOTS[0]) / raw_path
        root = raw_path.resolve()
        if not any(
            root.is_relative_to(Path(item).expanduser().resolve())
            for item in MCP_ALLOWED_ROOTS
            if item
        ):
            raise PermissionError(f"扫描目录不在允许范围内：{root}")
        if not root.is_dir():
            raise FileNotFoundError(f"扫描目录不存在：{root}")
        iterator = root.rglob(job.file_pattern) if job.recursive else root.glob(job.file_pattern)
        processed = 0
        active_source_objects: set[str] = set()
        complete_snapshot = True
        for path in iterator:
            if self._is_cancelled(job.id):
                return
            if processed >= AUTO_SCAN_MAX_FILES_PER_RUN:
                complete_snapshot = False
                break
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            item_key = f"local:{path.resolve()}"
            source_object = str(path.resolve())
            active_source_objects.add(source_object)
            if self._already_seen(job, item_key, digest):
                continue
            result = self._ingest(
                db,
                job,
                filename=path.name,
                content=content,
                space_id=space_id,
                source_object=source_object,
                source_context={"kind": "file", "path": str(path.resolve())},
            )
            if result.status == "success":
                self._mark_seen(job, item_key, digest)
                db.commit()
                processed += 1
        if complete_snapshot and not self._is_cancelled(job.id):
            self._prune_missing_source_records(
                db,
                job,
                source_id=source.id,
                active_source_objects=active_source_objects,
            )

    def _scan_remote(
        self,
        db,
        job: AutoScanJob,
        source: DataSource,
        space_id: str,
    ) -> None:
        config = resolved_source_config(source)
        host = str(config.get("host", ""))
        port = int(config.get("port") or (22 if job.scan_type == "sftp" else 21))
        username = str(config.get("username", ""))
        password = str(config.get("password", ""))
        private_key = str(config.get("private_key") or "")
        remote_path = job.path or str(config.get("path", "."))
        if not host:
            raise ValueError("数据源缺少 FTP/SFTP 主机")
        if job.scan_type == "sftp":
            try:
                import paramiko
            except ImportError as error:
                raise RuntimeError("SFTP 扫描需要 paramiko") from error
            transport = paramiko.Transport((host, port))
            transport.connect(
                username=username,
                password=password if not private_key else None,
                pkey=load_sftp_private_key(private_key) if private_key else None,
            )
            client = paramiko.SFTPClient.from_transport(transport)
            try:
                candidates: list[tuple[str, str]] = []

                def walk_sftp(path: str) -> None:
                    if self._is_cancelled(job.id):
                        return
                    for attr in client.listdir_attr(path):
                        name = attr.filename
                        full_path = posixpath.join(path, name)
                        if attr.st_mode and stat.S_ISDIR(attr.st_mode):
                            if job.recursive:
                                walk_sftp(full_path)
                            continue
                        if self._allowed_filename(name, job.file_pattern):
                            candidates.append((name, full_path))

                walk_sftp(remote_path)
                processed = 0
                active_source_objects: set[str] = set()
                complete_snapshot = True
                for filename, full_path in candidates:
                    if self._is_cancelled(job.id):
                        return
                    with client.open(full_path, "rb") as handle:
                        content = handle.read()
                    digest = hashlib.sha256(content).hexdigest()
                    item_key = f"sftp:{host}:{full_path}"
                    active_source_objects.add(full_path)
                    if self._already_seen(job, item_key, digest):
                        continue
                    result = self._ingest(
                        db,
                        job,
                        filename=filename,
                        content=content,
                        space_id=space_id,
                        source_object=full_path,
                        source_context={"kind": "sftp", "path": full_path},
                    )
                    if result.status == "success":
                        self._mark_seen(job, item_key, digest)
                        db.commit()
                        processed += 1
                    if processed >= AUTO_SCAN_MAX_FILES_PER_RUN:
                        complete_snapshot = False
                        break
                if complete_snapshot and not self._is_cancelled(job.id):
                    self._prune_missing_source_records(
                        db,
                        job,
                        source_id=source.id,
                        active_source_objects=active_source_objects,
                    )
            finally:
                client.close()
                transport.close()
        else:
            ftp = FTP_TLS() if config.get("use_tls") else FTP()
            ftp.connect(host, port, timeout=30)
            ftp.login(username or "anonymous", password or "")
            if isinstance(ftp, FTP_TLS):
                ftp.prot_p()
            try:
                candidates: list[tuple[str, str]] = []

                def walk_ftp(path: str) -> None:
                    if self._is_cancelled(job.id):
                        return
                    try:
                        entries = list(ftp.mlsd(path))
                    except Exception as error:
                        if job.recursive:
                            raise RuntimeError(
                                "当前 FTP 服务器不支持递归目录扫描"
                            ) from error
                        entries = [
                            (Path(item).name, {"type": "file"})
                            for item in ftp.nlst(path)
                        ]
                    for name, facts in entries:
                        if name in {".", ".."}:
                            continue
                        full_path = posixpath.join(path, name)
                        if facts.get("type") == "dir":
                            if job.recursive:
                                walk_ftp(full_path)
                            continue
                        if self._allowed_filename(name, job.file_pattern):
                            candidates.append((name, full_path))

                walk_ftp(remote_path)
                processed = 0
                active_source_objects: set[str] = set()
                complete_snapshot = True
                for filename, full_path in candidates:
                    if self._is_cancelled(job.id):
                        return
                    buffer = io.BytesIO()
                    ftp.retrbinary(f"RETR {full_path}", buffer.write)
                    content = buffer.getvalue()
                    digest = hashlib.sha256(content).hexdigest()
                    item_key = f"ftp:{host}:{full_path}"
                    active_source_objects.add(full_path)
                    if self._already_seen(job, item_key, digest):
                        continue
                    result = self._ingest(
                        db,
                        job,
                        filename=filename,
                        content=content,
                        space_id=space_id,
                        source_object=full_path,
                        source_context={"kind": "ftp", "path": full_path},
                    )
                    if result.status == "success":
                        self._mark_seen(job, item_key, digest)
                        db.commit()
                        processed += 1
                    if processed >= AUTO_SCAN_MAX_FILES_PER_RUN:
                        complete_snapshot = False
                        break
                if complete_snapshot and not self._is_cancelled(job.id):
                    self._prune_missing_source_records(
                        db,
                        job,
                        source_id=source.id,
                        active_source_objects=active_source_objects,
                    )
            finally:
                ftp.quit()

    def _scan_database(
        self,
        db,
        job: AutoScanJob,
        source: DataSource,
        space_id: str,
    ) -> None:
        config = resolved_source_config(source)
        source_type = source.source_type
        if source_type not in {"mysql", "postgres", "postgresql"}:
            raise ValueError("数据库扫描需要 MySQL 或 PostgreSQL 数据源")
        table_names = configured_table_names(config)
        if not table_names:
            raise ValueError("请选择要同步的数据库表")
        if len(table_names) != 1:
            raise ValueError("一个数据库数据源只能绑定一张表")
        job.scan_state = {
            **(job.scan_state or {}),
            "unchanged": False,
        }
        engine = create_engine(
            database_url(source_type, config),
            pool_pre_ping=True,
        )
        snapshots: list[tuple[str, list[dict[str, Any]]]] = []
        primary_keys_by_table: dict[str, list[str]] = {}
        try:
            with engine.connect() as connection:
                inspector = sqlalchemy_inspect(engine)
                schema_name = str(config.get("schema_name") or "").strip() or None
                for table_name in table_names:
                    if self._is_cancelled(job.id):
                        return
                    primary_keys_by_table[table_name] = [
                        str(item)
                        for item in (
                            inspector.get_pk_constraint(
                                table_name,
                                schema=schema_name,
                            )
                            or {}
                        ).get("constrained_columns", [])
                    ]
                    query_config = {
                        **config,
                        "table_name": table_name,
                        "sync_mode": "full",
                        "watermark_column": "",
                        "initial_cursor": "",
                    }
                    query = database_source_query(engine, query_config)
                    if not query.lower().startswith("select"):
                        raise ValueError("数据库扫描只允许配置 SELECT 查询")
                    result = connection.execute(text(query))
                    rows = [dict(row) for row in result.mappings().all()]
                    rows.sort(
                        key=lambda row: json.dumps(
                            row,
                            sort_keys=True,
                            default=str,
                            ensure_ascii=False,
                        )
                    )
                    snapshots.append(
                        (
                            table_name,
                            rows,
                        )
                    )
        finally:
            engine.dispose()
        dataset = db.get(Dataset, job.dataset_id)
        if (
            dataset is not None
            and not list(dataset.upsert_keys or [])
            and snapshots
            and snapshots[0][1]
        ):
            primary_keys = [
                key
                for key in primary_keys_by_table.get(snapshots[0][0], [])
                if key in snapshots[0][1][0]
            ]
            if primary_keys:
                dataset.upsert_keys = primary_keys
                db.commit()
        active_source_objects: set[str] = set()
        for source_object, rows in snapshots:
            if self._is_cancelled(job.id):
                return
            active_source_objects.add(source_object)
            item_key = f"database:{job.id}:{source_object}"
            if not rows:
                empty_digest = hashlib.sha256(
                    f"[]:{source_object}".encode("utf-8")
                ).hexdigest()
                if self._already_seen(job, item_key, empty_digest):
                    continue
                self._clear_source_object_snapshot(
                    db,
                    job,
                    source_id=source.id,
                    source_object=source_object,
                )
                self._mark_seen(job, item_key, empty_digest)
                db.commit()
                continue
            columns = sorted(
                {
                    str(key)
                    for row in rows
                    for key in row
                }
            )
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {key: _json_value(row.get(key)) for key in columns}
                )
            content = buffer.getvalue().encode("utf-8-sig")
            digest = hashlib.sha256(content).hexdigest()
            if self._already_seen(job, item_key, digest) or self._latest_database_digest_matches(
                db,
                job,
                source_object=source_object,
                digest=digest,
            ):
                self._mark_seen(job, item_key, digest)
                job.scan_state = {
                    **(job.scan_state or {}),
                    "unchanged": True,
                }
                db.commit()
                continue
            filename = f"{job.id}_{int(time.time())}.csv"
            run = self._ingest(
                db,
                job,
                filename=filename,
                content=content,
                space_id=space_id,
                source_object=source_object,
                source_context={
                    "kind": "database",
                    "table_name": source_object,
                    "schema_name": str(config.get("schema_name") or ""),
                    "primary_key_fields": primary_keys_by_table.get(
                        source_object,
                        [],
                    ),
                    "dedupe_scope": source_object,
                    "table_digest": digest,
                },
                replace_source_object=True,
                dedupe_existing_file=False,
            )
            if run.status == "success":
                self._mark_seen(job, item_key, digest)
                job.scan_state = {
                    **(job.scan_state or {}),
                    "unchanged": False,
                }
                db.commit()
        self._prune_missing_source_records(
            db,
            job,
            source_id=source.id,
            active_source_objects=active_source_objects,
        )

    def _scan_api(
        self,
        db,
        job: AutoScanJob,
        source: DataSource,
        space_id: str,
    ) -> None:
        config = resolved_source_config(source)
        if self._is_cancelled(job.id):
            return
        filename, content, row_count = request_api_source(
            config,
            source_name=source.name,
        )
        digest = hashlib.sha256(content).hexdigest()
        item_key = f"api:{source.id}:{config.get('url', '')}"
        source_object = str(config.get("url") or source.name)
        if self._already_seen(job, item_key, digest):
            return
        if row_count == 0:
            self._clear_source_object_snapshot(
                db,
                job,
                source_id=source.id,
                source_object=source_object,
            )
            self._mark_seen(job, item_key, digest)
            db.commit()
            return
        result = self._ingest(
            db,
            job,
            filename=filename,
            content=content,
            space_id=space_id,
            source_object=source_object,
            source_context={"kind": "api", "url": str(config.get("url") or "")},
        )
        if result.status == "success":
            self._mark_seen(job, item_key, digest)
            db.commit()

    def _ingest(
        self,
        db,
        job: AutoScanJob,
        *,
        filename: str,
        content: bytes,
        space_id: str,
        source_object: str = "",
        source_context: dict[str, Any] | None = None,
        replace_source_object: bool = True,
        dedupe_existing_file: bool = True,
    ) -> IngestionRun:
        template_code = job.template_code
        if template_code == "auto":
            inspection = inspect_file(filename, content)
            template_code = inspection["recommended_pipeline"]
        duplicate = (
            find_duplicate_ingestion_run(
                db,
                dataset_id=job.dataset_id,
                fingerprint=content_fingerprint(content),
            )
            if dedupe_existing_file
            else None
        )
        if duplicate is not None:
            job.last_run_id = duplicate.id
            job.scan_state = {
                **(job.scan_state or {}),
                "unchanged": True,
            }
            db.commit()
            return duplicate
        run = create_ingestion_run(
            db,
            filename=filename,
            content=content,
            dataset_id=job.dataset_id,
            template_code=template_code,
            source_id=job.source_id,
            space_id=space_id,
            source_object=source_object,
            source_context=source_context,
        )
        snapshot_fingerprints = run_pipeline(run.id, space_id)
        db.refresh(run)
        job.last_run_id = run.id
        if run.status == "success":
            if replace_source_object and snapshot_fingerprints is not None:
                self._replace_source_object_records(
                    db,
                    job,
                    source_object=run.source_object,
                    fingerprints=snapshot_fingerprints,
                )
            job.processed_count += 1
        elif run.error_message:
            job.last_error = job.last_error or run.error_message
        db.commit()
        return run

    @staticmethod
    def _refresh_dataset_record_count(db, dataset: Dataset) -> None:
        if dataset.physical_table:
            dataset.record_count = count_dedicated_records(
                db.get_bind(),
                dataset.physical_table,
            )
            return
        dataset.record_count = int(
            db.scalar(
                select(func.count(DatasetRecord.id)).where(
                    DatasetRecord.dataset_id == dataset.id
                )
            )
            or 0
        )

    def _replace_source_object_records(
        self,
        db,
        job: AutoScanJob,
        *,
        source_object: str,
        fingerprints: set[str],
    ) -> None:
        dataset = db.get(Dataset, job.dataset_id)
        if dataset is None:
            return
        if dataset.physical_table:
            delete_dedicated_source_records(
                db.get_bind(),
                dataset.physical_table,
                source_id=job.source_id,
                source_object=source_object,
                keep_fingerprints=fingerprints,
            )
        else:
            stale_ids = [
                record.id
                for record in db.query(
                    DatasetRecord.id,
                    DatasetRecord.fingerprint,
                )
                .filter(
                    DatasetRecord.dataset_id == job.dataset_id,
                    DatasetRecord.source_id == job.source_id,
                    DatasetRecord.source_object == source_object,
                )
                .all()
                if record.fingerprint not in fingerprints
            ]
            if stale_ids:
                db.query(DatasetRecord).filter(
                    DatasetRecord.id.in_(stale_ids)
                ).delete(synchronize_session=False)
        self._refresh_dataset_record_count(db, dataset)

    def _clear_source_object_snapshot(
        self,
        db,
        job: AutoScanJob,
        *,
        source_id: str,
        source_object: str,
    ) -> None:
        dataset = db.get(Dataset, job.dataset_id)
        if dataset is None:
            return
        if dataset.physical_table:
            delete_dedicated_source_records(
                db.get_bind(),
                dataset.physical_table,
                source_id=source_id,
                source_object=source_object,
            )
        else:
            db.query(DatasetRecord).filter(
                DatasetRecord.dataset_id == job.dataset_id,
                DatasetRecord.source_id == source_id,
                DatasetRecord.source_object == source_object,
            ).delete(synchronize_session=False)
        self._refresh_dataset_record_count(db, dataset)

    def _prune_missing_source_records(
        self,
        db,
        job: AutoScanJob,
        *,
        source_id: str,
        active_source_objects: set[str],
    ) -> None:
        dataset = db.get(Dataset, job.dataset_id)
        if dataset is None:
            return
        if dataset.physical_table:
            prune_dedicated_source_records(
                db.get_bind(),
                dataset.physical_table,
                source_id=source_id,
                active_source_objects=active_source_objects,
            )
        else:
            query = db.query(DatasetRecord).filter(
                DatasetRecord.dataset_id == job.dataset_id,
                DatasetRecord.source_id == source_id,
            )
            if active_source_objects:
                query = query.filter(
                    DatasetRecord.source_object.notin_(active_source_objects)
                )
            query.delete(synchronize_session=False)
        self._refresh_dataset_record_count(db, dataset)

    @staticmethod
    def _allowed_filename(filename: str, pattern: str) -> bool:
        return (
            Path(filename).suffix.lower() in SUPPORTED_SUFFIXES
            and fnmatch.fnmatch(filename, pattern or "*")
        )

    @staticmethod
    def _already_seen(job: AutoScanJob, item_key: str, digest: str) -> bool:
        seen = (job.scan_state or {}).get("seen", {})
        return seen.get(item_key) == digest

    @staticmethod
    def _latest_database_digest_matches(
        db,
        job: AutoScanJob,
        *,
        source_object: str,
        digest: str,
    ) -> bool:
        dataset = db.get(Dataset, job.dataset_id)
        if dataset is None or int(dataset.record_count or 0) <= 0:
            return False
        latest_run = db.scalar(
            select(IngestionRun)
            .where(
                IngestionRun.dataset_id == job.dataset_id,
                IngestionRun.source_id == job.source_id,
                IngestionRun.source_object == source_object,
                IngestionRun.status == "success",
            )
            .order_by(IngestionRun.created_at.desc())
            .limit(1)
        )
        return bool(
            latest_run
            and str((latest_run.source_context or {}).get("table_digest") or "")
            == digest
        )

    @staticmethod
    def _mark_seen(job: AutoScanJob, item_key: str, digest: str) -> None:
        state = dict(job.scan_state or {})
        seen = dict(state.get("seen", {}))
        seen[item_key] = digest
        if len(seen) > 2000:
            seen = dict(list(seen.items())[-2000:])
        state["seen"] = seen
        job.scan_state = state


auto_scan_scheduler = AutoScanScheduler()
