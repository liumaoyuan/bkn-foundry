from __future__ import annotations

from collections.abc import Generator
from hashlib import sha256
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

from .config import APP_ENV, DATABASE_URL
from .models import Base, DataSpace
from .secret_store import decrypt_secret, encrypt_secret


DEFAULT_SPACE_ID = "default"
ALLOWED_DATABASE_SCHEMES = {
    "mysql",
    "sqlite",
}


def _engine_options(database_url: str) -> dict[str, Any]:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    return options


engine = create_engine(DATABASE_URL, **_engine_options(DATABASE_URL))
SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)
_space_engine_cache: dict[str, Engine] = {}
_space_session_cache: dict[str, sessionmaker[Session]] = {}


def init_database() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_auto_scan_columns(engine)
    _ensure_agent_session_columns(engine)
    _ensure_default_space()
    for space in list_data_spaces():
        try:
            initialize_space_database(space)
            _set_space_status(space.id, "ready")
        except Exception as error:
            _set_space_status(space.id, "error", _safe_error(error))


def _ensure_default_space() -> None:
    db = SessionLocal()
    try:
        if db.get(DataSpace, DEFAULT_SPACE_ID) is not None:
            return
        db.add(
            DataSpace(
                id=DEFAULT_SPACE_ID,
                name="默认数据空间",
                slug=DEFAULT_SPACE_ID,
                database_url_encrypted=encrypt_secret(DATABASE_URL),
                database_label=database_label(DATABASE_URL),
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
    if parsed.get_backend_name() not in ALLOWED_DATABASE_SCHEMES:
        raise ValueError("仅支持 MySQL/MariaDB 或本地开发用 SQLite")
    if parsed.get_backend_name() == "mysql" and parsed.drivername != "mysql+pymysql":
        raise ValueError("MySQL/MariaDB 必须使用 mysql+pymysql 驱动")
    if APP_ENV == "production" and parsed.get_backend_name() == "sqlite":
        raise ValueError("生产环境的数据空间只支持 MySQL/MariaDB")
    return value


def _space_database_url(space: DataSpace) -> str:
    if space.id == DEFAULT_SPACE_ID:
        return DATABASE_URL
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
    reference = (space_reference or DEFAULT_SPACE_ID).strip()
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
    reference = (space_reference or DEFAULT_SPACE_ID).strip()
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
    return str(getattr(request.state, "data_space_id", DEFAULT_SPACE_ID))


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


def get_db(request: Request) -> Generator[Session, None, None]:
    space_reference = (
        request.headers.get("x-data-ingestion-space")
        or request.query_params.get("space_id")
        or DEFAULT_SPACE_ID
    )
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
