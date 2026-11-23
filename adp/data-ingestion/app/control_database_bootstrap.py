from __future__ import annotations

import re
import threading
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from .config import CONTROL_DATABASE_BOOTSTRAP_ENABLED
from .database import (
    control_database_status,
    control_database_url,
    configure_control_database,
    database_label,
    init_database,
    persist_control_database_config,
    reset_control_database,
    validate_database_url,
)


class ControlDatabaseBootstrapError(ValueError):
    pass


class ControlDatabaseAlreadyInitialized(ControlDatabaseBootstrapError):
    pass


_bootstrap_lock = threading.Lock()
_DATABASE_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")


def _database_details(database_url: str):
    value = validate_database_url(database_url)
    try:
        parsed = make_url(value)
    except ArgumentError as error:
        raise ControlDatabaseBootstrapError("数据库连接地址格式无效") from error
    database_name = (parsed.database or "").strip()
    if not database_name:
        raise ControlDatabaseBootstrapError("数据库连接地址必须包含目标数据库名")
    if parsed.get_backend_name() in {"mysql", "mariadb"} and not (
        _DATABASE_NAME_PATTERN.fullmatch(database_name)
    ):
        raise ControlDatabaseBootstrapError(
            "数据库名只能包含字母、数字和下划线"
        )
    return value, parsed, database_name


def test_control_database(database_url: str) -> dict[str, str | bool]:
    value, parsed, database_name = _database_details(database_url)
    backend = parsed.get_backend_name()
    if backend == "sqlite":
        sqlite_path = database_name
        exists = Path(sqlite_path).is_file() if sqlite_path else True
        probe_engine = create_engine(value, pool_pre_ping=True)
        try:
            with probe_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            probe_engine.dispose()
        return {
            "connected": True,
            "database_exists": exists,
            "database_name": sqlite_path,
            "database_label": database_label(value),
        }

    server_url = parsed.set(database=None)
    probe_engine = create_engine(
        server_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
    try:
        with probe_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            exists = connection.scalar(
                text("SHOW DATABASES LIKE :database"),
                {"database": database_name},
            )
    finally:
        probe_engine.dispose()
    return {
        "connected": True,
        "database_exists": exists is not None,
        "database_name": database_name,
        "database_label": database_label(value),
    }


def _ensure_database_exists(
    database_url: str,
    *,
    create_database: bool,
    bootstrap_database_url: str,
) -> None:
    _, parsed, database_name = _database_details(database_url)
    backend = parsed.get_backend_name()
    if backend == "sqlite":
        sqlite_path = Path(parsed.database or "")
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return

    setup_value = (
        _database_details(bootstrap_database_url)[0]
        if bootstrap_database_url.strip()
        else database_url
    )
    setup_url = make_url(setup_value).set(database=None)
    setup_engine = create_engine(
        setup_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
    try:
        with setup_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            exists = connection.scalar(
                text("SHOW DATABASES LIKE :database"),
                {"database": database_name},
            )
            if exists is not None:
                return
            if not create_database:
                raise ControlDatabaseBootstrapError(
                    f"目标数据库 {database_name} 不存在"
                )
            connection.execute(
                text(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
    finally:
        setup_engine.dispose()


def initialize_user_control_database(
    database_url: str,
    *,
    create_database: bool = True,
    bootstrap_database_url: str = "",
) -> None:
    if not CONTROL_DATABASE_BOOTSTRAP_ENABLED:
        raise ControlDatabaseBootstrapError("前端控制库初始化功能已关闭")

    with _bootstrap_lock:
        status = control_database_status()
        if status.source == "environment":
            raise ControlDatabaseAlreadyInitialized(
                "数据入库控制库由部署环境管理，不能在页面中更改"
            )

        app_value, _, _ = _database_details(database_url)
        previous_url = control_database_url()
        previous_source = status.source
        _ensure_database_exists(
            app_value,
            create_database=create_database,
            bootstrap_database_url=bootstrap_database_url,
        )
        configure_control_database(app_value, source="user")
        try:
            init_database()
            from .database import SessionLocal
            from .seed import seed_database

            db = SessionLocal()
            try:
                seed_database(db, include_examples=False)
            finally:
                db.close()
            persist_control_database_config(app_value)
        except Exception:
            reset_control_database()
            if previous_url:
                configure_control_database(
                    previous_url,
                    source=previous_source,
                )
                init_database()
            raise
