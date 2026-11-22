from __future__ import annotations

import asyncio
import csv
import fnmatch
import hashlib
import io
import logging
import time
from datetime import UTC, datetime, timedelta
from ftplib import FTP, FTP_TLS
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import create_engine, or_, select, text

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
from .ingestion_service import IngestionValidationError, create_ingestion_run
from .models import AutoScanJob, DataSource, IngestionRun, utc_now
from .pipeline import run_pipeline


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

    async def start(self) -> None:
        if not AUTO_SCAN_ENABLED or self.task:
            return
        self.task = asyncio.create_task(self._loop(), name="auto-scan-scheduler")

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
    ) -> None:
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

    def _scan_job_sync(
        self,
        job_id: str,
        space_id: str = DEFAULT_SPACE_ID,
    ) -> None:
        db = session_for_space(space_id)
        try:
            job = db.get(AutoScanJob, job_id)
            if job is None or not job.enabled:
                return
            source = db.get(DataSource, job.source_id)
            if source is None:
                raise RuntimeError("扫描任务关联的数据源不存在")
            if job.scan_type == "local":
                self._scan_local(db, job, source, space_id)
            elif job.scan_type in {"ftp", "sftp"}:
                self._scan_remote(db, job, source, space_id)
            elif job.scan_type == "database":
                self._scan_database(db, job, source, space_id)
            else:
                raise ValueError(f"不支持的扫描类型：{job.scan_type}")
            job.status = "ready"
            job.last_error = ""
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
        for path in iterator:
            if processed >= AUTO_SCAN_MAX_FILES_PER_RUN:
                break
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            item_key = f"local:{path.resolve()}"
            if self._already_seen(job, item_key, digest):
                continue
            result = self._ingest(
                db,
                job,
                filename=path.name,
                content=content,
                space_id=space_id,
            )
            if result.status == "success":
                self._mark_seen(job, item_key, digest)
                db.commit()
                processed += 1

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
        remote_path = job.path or str(config.get("path", "."))
        if not host:
            raise ValueError("数据源缺少 FTP/SFTP 主机")
        files: list[tuple[str, bytes]] = []
        if job.scan_type == "sftp":
            try:
                import paramiko
            except ImportError as error:
                raise RuntimeError("SFTP 扫描需要 paramiko") from error
            transport = paramiko.Transport((host, port))
            transport.connect(username=username, password=password)
            client = paramiko.SFTPClient.from_transport(transport)
            try:
                for attr in client.listdir_attr(remote_path):
                    name = attr.filename
                    if not self._allowed_filename(name, job.file_pattern):
                        continue
                    with client.open(f"{remote_path.rstrip('/')}/{name}", "rb") as handle:
                        files.append((name, handle.read()))
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
                ftp.cwd(remote_path)
                for name in ftp.nlst():
                    clean_name = Path(name).name
                    if not self._allowed_filename(clean_name, job.file_pattern):
                        continue
                    buffer = io.BytesIO()
                    ftp.retrbinary(f"RETR {clean_name}", buffer.write)
                    files.append((clean_name, buffer.getvalue()))
            finally:
                ftp.quit()
        for filename, content in files[:AUTO_SCAN_MAX_FILES_PER_RUN]:
            digest = hashlib.sha256(content).hexdigest()
            item_key = f"{job.scan_type}:{host}:{remote_path.rstrip('/')}/{filename}"
            if self._already_seen(job, item_key, digest):
                continue
            result = self._ingest(
                db,
                job,
                filename=filename,
                content=content,
                space_id=space_id,
            )
            if result.status == "success":
                self._mark_seen(job, item_key, digest)
                db.commit()

    def _scan_database(
        self,
        db,
        job: AutoScanJob,
        source: DataSource,
        space_id: str,
    ) -> None:
        config = resolved_source_config(source)
        query = str(config.get("query", "")).strip()
        if not query.lower().startswith("select"):
            raise ValueError("数据库扫描只允许配置 SELECT 查询")
        source_type = source.source_type
        if source_type == "mysql":
            url = (
                "mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
                "?charset=utf8mb4"
            ).format(
                user=config.get("username", ""),
                password=config.get("password", ""),
                host=config.get("host", "127.0.0.1"),
                port=config.get("port", 3306),
                database=config.get("database", ""),
            )
        elif source_type in {"postgres", "postgresql"}:
            url = "postgresql+psycopg://{user}:{password}@{host}:{port}/{database}".format(
                user=config.get("username", ""),
                password=config.get("password", ""),
                host=config.get("host", "127.0.0.1"),
                port=config.get("port", 5432),
                database=config.get("database", ""),
            )
        else:
            raise ValueError("数据库扫描需要 MySQL 或 PostgreSQL 数据源")
        watermark = str(config.get("watermark_column", "")).strip()
        params: dict[str, Any] = {}
        if watermark:
            cursor = (job.scan_state or {}).get("cursor")
            params["cursor"] = cursor or "1970-01-01T00:00:00"
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                result = connection.execute(text(query), params)
                rows = [dict(row) for row in result.mappings().all()]
        finally:
            engine.dispose()
        if not rows:
            return
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _json_value(row.get(key)) for key in columns})
        content = buffer.getvalue().encode("utf-8-sig")
        digest = hashlib.sha256(content).hexdigest()
        if self._already_seen(job, f"database:{job.id}", digest):
            return
        filename = f"{job.id}_{int(time.time())}.csv"
        run = self._ingest(
            db,
            job,
            filename=filename,
            content=content,
            space_id=space_id,
        )
        if run.status == "success":
            self._mark_seen(job, f"database:{job.id}", digest)
            if watermark:
                state = dict(job.scan_state or {})
                state["cursor"] = _json_value(rows[-1].get(watermark))
                job.scan_state = state
            db.commit()

    def _ingest(
        self,
        db,
        job: AutoScanJob,
        *,
        filename: str,
        content: bytes,
        space_id: str,
    ) -> IngestionRun:
        template_code = job.template_code
        if template_code == "auto":
            inspection = inspect_file(filename, content)
            template_code = inspection["recommended_pipeline"]
        run = create_ingestion_run(
            db,
            filename=filename,
            content=content,
            dataset_id=job.dataset_id,
            template_code=template_code,
            source_id=job.source_id,
            space_id=space_id,
        )
        run_pipeline(run.id, space_id)
        db.refresh(run)
        job.last_run_id = run.id
        if run.status == "success":
            job.processed_count += 1
        job.last_error = "" if run.status == "success" else run.error_message
        db.commit()
        return run

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
    def _mark_seen(job: AutoScanJob, item_key: str, digest: str) -> None:
        state = dict(job.scan_state or {})
        seen = dict(state.get("seen", {}))
        seen[item_key] = digest
        if len(seen) > 2000:
            seen = dict(list(seen.items())[-2000:])
        state["seen"] = seen
        job.scan_state = state


auto_scan_scheduler = AutoScanScheduler()
