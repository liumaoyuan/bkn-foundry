from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

from .config import (
    ALLOW_SQLITE_DATABASE,
    APP_ENV,
    CONTROL_DATABASE_CONFIG_PATH,
    DATABASE_URL,
    ENV_DATABASE_URL,
)
from .models import Base, DataSpace
from .secret_store import decrypt_secret, encrypt_secret


DEFAULT_SPACE_ID = "default"
ALLOWED_DATABASE_SCHEMES = {
    "mysql",
    "mariadb",
    "postgresql",
}
SUPPORTED_DATABASE_DRIVERS = {
    "mysql": {"mysql+pymysql"},
    "mariadb": {"mariadb+pymysql"},
    "postgresql": {"postgresql+psycopg"},
}


@dataclass(frozen=True)
class ControlDatabaseStatus:
    configured: bool
    initialized: bool
    source: str
    database_label: str = ""
    error: str = ""


class ControlDatabaseNotInitialized(RuntimeError):
    pass


def _engine_options(database_url: str) -> dict[str, Any]:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    return options


engine: Engine | None = None
SessionLocal = sessionmaker(
    bind=None,
    class_=Session,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)
_control_database_lock = threading.RLock()
_control_database_url = ""
_control_database_source = "uninitialized"
_control_database_initialized = False
_control_database_error = ""
_space_engine_cache: dict[str, Engine] = {}
_space_session_cache: dict[str, sessionmaker[Session]] = {}


def control_database_status() -> ControlDatabaseStatus:
    return ControlDatabaseStatus(
        configured=bool(_control_database_url),
        initialized=_control_database_initialized,
        source=_control_database_source,
        database_label=(
            database_label(_control_database_url)
            if _control_database_url
            else ""
        ),
        error=_control_database_error,
    )


def control_database_url() -> str:
    return _control_database_url


def configure_control_database(
    database_url: str,
    *,
    source: str,
    persist: bool = False,
) -> None:
    """Replace the control database engine without restarting the service."""
    value = validate_database_url(database_url)
    candidate = create_engine(value, **_engine_options(value))
    with _control_database_lock:
        global engine, _control_database_url
        global _control_database_source, _control_database_initialized
        global _control_database_error
        previous = engine
        engine = candidate
        _control_database_url = value
        _control_database_source = source
        _control_database_initialized = False
        _control_database_error = ""
        SessionLocal.configure(bind=candidate)
        if persist:
            persist_control_database_config(value)
        if previous is not None and previous is not candidate:
            previous.dispose()


def reset_control_database() -> None:
    """Return to the uninitialized state, primarily for recovery and tests."""
    with _control_database_lock:
        global engine, _control_database_url
        global _control_database_source, _control_database_initialized
        global _control_database_error
        previous = engine
        engine = None
        _control_database_url = ""
        _control_database_source = "uninitialized"
        _control_database_initialized = False
        _control_database_error = ""
        SessionLocal.configure(bind=None)
        for configured_engine in _space_engine_cache.values():
            configured_engine.dispose()
        _space_engine_cache.clear()
        _space_session_cache.clear()
        if previous is not None:
            previous.dispose()


def require_control_database() -> Engine:
    if engine is None:
        raise ControlDatabaseNotInitialized("数据入库控制库尚未初始化")
    return engine


def require_initialized_control_database() -> None:
    status = control_database_status()
    if not status.configured or not status.initialized:
        raise HTTPException(
            status_code=503,
            detail="数据入库控制库尚未初始化",
        )


def persist_control_database_config(database_url: str) -> None:
    path = CONTROL_DATABASE_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "source": "user",
        "database_url_encrypted": encrypt_secret(database_url),
        "database_label": database_label(database_url),
        "created_at": datetime.now(UTC).isoformat(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    try:
        os.replace(temporary, path)
    except OSError:
        # Some Windows workspace sandboxes reject replacing files in managed
        # temporary directories even though writing the target is permitted.
        path.write_text(temporary.read_text(encoding="utf-8"), encoding="utf-8")
        temporary.unlink(missing_ok=True)


def load_persisted_control_database_config() -> str:
    path = CONTROL_DATABASE_CONFIG_PATH
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("控制库配置文件无法读取") from error
    if not isinstance(payload, dict):
        raise ValueError("控制库配置文件格式无效")
    encrypted = str(payload.get("database_url_encrypted") or "").strip()
    if not encrypted:
        raise ValueError("控制库配置文件缺少数据库连接信息")
    try:
        database_url = decrypt_secret(encrypted)
    except Exception as error:
        raise ValueError("控制库连接信息无法解密") from error
    return validate_database_url(database_url)


def _configure_control_database_from_environment() -> None:
    global _control_database_error
    if ENV_DATABASE_URL:
        configure_control_database(ENV_DATABASE_URL, source="environment")
        return
    try:
        persisted_url = load_persisted_control_database_config()
    except ValueError as error:
        _control_database_error = str(error)
        return
    if persisted_url:
        configure_control_database(persisted_url, source="user")
        return
    if DATABASE_URL:
        configure_control_database(DATABASE_URL, source="development")


def init_database() -> None:
    configured_engine = require_control_database()
    Base.metadata.create_all(bind=configured_engine)
    _ensure_auto_scan_columns(configured_engine)
    _ensure_agent_session_columns(configured_engine)
    _ensure_agent_message_columns(configured_engine)
    _ensure_dataset_storage_columns(configured_engine)
    _ensure_ingestion_columns(configured_engine)
    _ensure_default_space()
    global _control_database_initialized, _control_database_error
    _control_database_initialized = True
    for space in list_data_spaces():
        try:
            space_engine = initialize_space_database(space)
            _ensure_dataset_storage_columns(space_engine)
            _ensure_ingestion_columns(space_engine)
            from .data_source_service import (
                migrate_plaintext_mcp_headers,
                migrate_plaintext_source_headers,
            )
            from .models import PipelineExecution, utc_now

            space_db = session_factory_for_space(space.id)()
            try:
                migrate_plaintext_source_headers(space_db)
                migrate_plaintext_mcp_headers(space_db)
                space_db.query(PipelineExecution).filter(
                    PipelineExecution.status == "running"
                ).update(
                    {
                        PipelineExecution.status: "failed",
                        PipelineExecution.error_message: "服务重启导致管线执行中断",
                        PipelineExecution.finished_at: utc_now(),
                    },
                    synchronize_session=False,
                )
                space_db.commit()
            finally:
                space_db.close()
            _set_space_status(space.id, "ready")
        except Exception as error:
            _set_space_status(space.id, "error", _safe_error(error))
    _control_database_error = ""


def _ensure_default_space() -> None:
    database_url = control_database_url()
    db = SessionLocal()
    try:
        if db.get(DataSpace, DEFAULT_SPACE_ID) is not None:
            return
        db.add(
            DataSpace(
                id=DEFAULT_SPACE_ID,
                name="默认数据空间",
                slug=DEFAULT_SPACE_ID,
                database_url_encrypted=encrypt_secret(database_url),
                database_label=database_label(database_url),
                is_default=True,
                status="ready",
            )
        )
        db.commit()
    finally:
        db.close()


def database_label(database_url: str) -> str:
    try:
        value = make_url(database_url)
        backend = value.get_backend_name()
        database = value.database or ""
        if backend == "sqlite":
            return f"sqlite:{database}"
        host = value.host or ""
        port = f":{value.port}" if value.port else ""
        return f"{backend}://{host}{port}/{database}"
    except Exception:
        return database_url.split("+", 1)[0]


def validate_database_url(database_url: str) -> str:
    value = database_url.strip()
    try:
        parsed = make_url(value)
    except ArgumentError as error:
        raise ValueError("数据库连接地址格式无效") from error
    backend = parsed.get_backend_name()
    if backend == "sqlite":
        if APP_ENV == "test" and ALLOW_SQLITE_DATABASE:
            return value
        raise ValueError("SQLite 仅用于内部测试，运行环境不支持 SQLite")
    if backend not in ALLOWED_DATABASE_SCHEMES:
        raise ValueError("仅支持 MySQL、MariaDB 或 PostgreSQL")
    if parsed.drivername not in SUPPORTED_DATABASE_DRIVERS[backend]:
        expected = "、".join(sorted(SUPPORTED_DATABASE_DRIVERS[backend]))
        raise ValueError(f"{backend} 必须使用 {expected} 驱动")
    return value


def _space_database_url(space: DataSpace) -> str:
    if space.id == DEFAULT_SPACE_ID:
        return control_database_url()
    return decrypt_secret(space.database_url_encrypted)


def initialize_space_database(space: DataSpace) -> Engine:
    configured_engine = _space_engine_cache.get(space.id)
    if configured_engine is not None:
        return configured_engine
    database_url = _space_database_url(space)
    configured_engine = create_engine(
        database_url,
        **_engine_options(database_url),
    )
    Base.metadata.create_all(bind=configured_engine)
    _ensure_auto_scan_columns(configured_engine)
    _ensure_agent_session_columns(configured_engine)
    _ensure_agent_message_columns(configured_engine)
    _ensure_dataset_storage_columns(configured_engine)
    _ensure_ingestion_columns(configured_engine)
    if space.id != DEFAULT_SPACE_ID:
        from .seed import seed_database

        seed_session = Session(configured_engine)
        try:
            seed_database(seed_session, include_examples=False)
        finally:
            seed_session.close()
    _space_engine_cache[space.id] = configured_engine
    return configured_engine


def session_factory_for_space(
    space_reference: str | None,
) -> sessionmaker[Session]:
    reference = (space_reference or "").strip()
    if not reference:
        raise ValueError("必须选择数据空间")
    space = resolve_data_space(reference)
    cached = _space_session_cache.get(space.id)
    if cached is not None:
        return cached
    configured_engine = initialize_space_database(space)
    configured_factory = sessionmaker(
        bind=configured_engine,
        class_=Session,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    _space_session_cache[space.id] = configured_factory
    return configured_factory


def session_for_space(space_reference: str | None = None) -> Session:
    return session_factory_for_space(space_reference)()


def resolve_data_space(space_reference: str | None) -> DataSpace:
    reference = (space_reference or "").strip()
    if not reference:
        raise LookupError("请选择数据空间")
    require_initialized_control_database()
    db = SessionLocal()
    try:
        space = db.get(DataSpace, reference)
        if space is None:
            space = db.query(DataSpace).filter(DataSpace.slug == reference).one_or_none()
        if space is None:
            raise LookupError("数据空间不存在")
        return space
    finally:
        db.close()


def list_data_spaces() -> list[DataSpace]:
    require_initialized_control_database()
    db = SessionLocal()
    try:
        return list(
            db.query(DataSpace)
            .order_by(DataSpace.is_default.desc(), DataSpace.name.asc())
            .all()
        )
    finally:
        db.close()


def current_space_id(request: Request) -> str:
    space_id = str(getattr(request.state, "data_space_id", "")).strip()
    if not space_id:
        raise HTTPException(status_code=400, detail="请选择数据空间")
    return space_id


def invalidate_space_cache(space_id: str) -> None:
    configured_engine = _space_engine_cache.pop(space_id, None)
    if configured_engine is not None:
        configured_engine.dispose()
    _space_session_cache.pop(space_id, None)


def _set_space_status(space_id: str, status: str, error: str = "") -> None:
    db = SessionLocal()
    try:
        space = db.get(DataSpace, space_id)
        if space is None:
            return
        space.status = status
        space.last_error = error
        db.commit()
    finally:
        db.close()


def _safe_error(error: Exception) -> str:
    digest = sha256(str(error).encode("utf-8")).hexdigest()[:12]
    return f"{type(error).__name__} [ref={digest}]"


def _ensure_auto_scan_columns(target_engine: Engine) -> None:
    inspector = inspect(target_engine)
    if "auto_scan_jobs" not in inspector.get_table_names():
        return
    existing = {
        column["name"]
        for column in inspector.get_columns("auto_scan_jobs")
    }
    dialect = target_engine.dialect.name
    json_type = "JSON" if dialect != "sqlite" else "TEXT"
    definitions = {
        "schedule_type": "VARCHAR(24) NOT NULL DEFAULT 'interval'",
        "schedule_time": "VARCHAR(5) NOT NULL DEFAULT '02:00'",
        "weekdays": f"{json_type} NULL",
        "cron_expression": "VARCHAR(160) NOT NULL DEFAULT ''",
        "timezone": "VARCHAR(80) NOT NULL DEFAULT 'Asia/Shanghai'",
    }
    with target_engine.begin() as connection:
        for column, definition in definitions.items():
            if column not in existing:
                connection.execute(
                    text(
                        f"ALTER TABLE auto_scan_jobs ADD COLUMN {column} {definition}"
                    )
                )
        connection.execute(
            text(
                "UPDATE auto_scan_jobs SET "
                "schedule_type = COALESCE(schedule_type, 'interval'), "
                "schedule_time = COALESCE(schedule_time, '02:00'), "
                "weekdays = COALESCE(weekdays, '[]'), "
                "cron_expression = COALESCE(cron_expression, ''), "
                "timezone = COALESCE(timezone, 'Asia/Shanghai')"
            )
        )


def _ensure_agent_session_columns(target_engine: Engine) -> None:
    inspector = inspect(target_engine)
    if "agent_sessions" not in inspector.get_table_names():
        return
    existing = {
        column["name"]
        for column in inspector.get_columns("agent_sessions")
    }
    if "owner_id" in existing:
        return
    with target_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE agent_sessions "
                "ADD COLUMN owner_id VARCHAR(128) NOT NULL DEFAULT ''"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX ix_agent_sessions_owner_id "
                "ON agent_sessions (owner_id)"
            )
        )


def _ensure_agent_message_columns(target_engine: Engine) -> None:
    inspector = inspect(target_engine)
    if "agent_messages" not in inspector.get_table_names():
        return
    existing = {
        column["name"]
        for column in inspector.get_columns("agent_messages")
    }
    if "attachments" in existing:
        return
    json_type = "JSON" if target_engine.dialect.name != "sqlite" else "TEXT"
    with target_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE agent_messages "
                f"ADD COLUMN attachments {json_type} NULL"
            )
        )


def _ensure_dataset_storage_columns(target_engine: Engine) -> None:
    inspector = inspect(target_engine)
    if "datasets" not in inspector.get_table_names():
        return
    existing = {
        column["name"]
        for column in inspector.get_columns("datasets")
    }
    additions = {
        "storage_mode": "VARCHAR(24) NOT NULL DEFAULT 'auto'",
        "physical_table": "VARCHAR(80) NOT NULL DEFAULT ''",
        "partition_mode": "VARCHAR(24) NOT NULL DEFAULT ''",
        "upsert_keys": "VARCHAR(1000) NOT NULL DEFAULT '[]'",
    }
    with target_engine.begin() as connection:
        for column, definition in additions.items():
            if column not in existing:
                connection.execute(
                    text(
                        f"ALTER TABLE datasets ADD COLUMN {column} {definition}"
                    )
                )


def _ensure_ingestion_columns(target_engine: Engine) -> None:
    inspector = inspect(target_engine)
    tables = set(inspector.get_table_names())
    json_type = "JSON" if target_engine.dialect.name != "sqlite" else "TEXT"
    additions = {
        "ingestion_runs": {
            "updated_count": "INTEGER NOT NULL DEFAULT 0",
            "source_object": "VARCHAR(255) NOT NULL DEFAULT ''",
            "source_context": f"{json_type} NULL",
            "incremental_cursor_before": "VARCHAR(255) NOT NULL DEFAULT ''",
            "incremental_cursor_after": "VARCHAR(255) NOT NULL DEFAULT ''",
        },
        "dataset_records": {
            "record_key": "VARCHAR(96) NOT NULL DEFAULT ''",
            "source_id": "VARCHAR(32) NULL",
            "source_object": "VARCHAR(255) NOT NULL DEFAULT ''",
            "source_locator": f"{json_type} NULL",
        },
    }
    with target_engine.begin() as connection:
        for table_name, columns in additions.items():
            if table_name not in tables:
                continue
            existing = {
                column["name"]
                for column in inspector.get_columns(table_name)
            }
            for column, definition in columns.items():
                if column not in existing:
                    connection.execute(
                        text(
                            f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}"
                        )
                    )


def get_db(request: Request) -> Generator[Session, None, None]:
    require_initialized_control_database()
    space_reference = (
        request.headers.get("x-data-ingestion-space")
        or request.query_params.get("space_id")
        or ""
    )
    if not space_reference.strip():
        raise HTTPException(status_code=400, detail="请选择数据空间")
    space: DataSpace | None = None
    try:
        space = resolve_data_space(space_reference)
        request.state.data_space_id = space.id
        request.state.data_space_slug = space.slug
        factory = session_factory_for_space(space.id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        if space is not None:
            _set_space_status(space.id, "error", _safe_error(error))
        raise HTTPException(
            status_code=503,
            detail="数据空间数据库不可用",
        ) from error
    db = factory()
    try:
        yield db
    finally:
        db.close()


_configure_control_database_from_environment()
