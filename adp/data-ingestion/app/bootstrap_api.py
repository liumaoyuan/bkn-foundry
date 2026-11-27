from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from .auto_scan import auto_scan_scheduler
from .config import CONTROL_DATABASE_BOOTSTRAP_ENABLED
from .control_database_bootstrap import (
    ControlDatabaseAlreadyInitialized,
    ControlDatabaseBootstrapError,
    initialize_user_control_database,
    test_control_database,
)
from .database import control_database_status
from .schemas import (
    ControlDatabaseBootstrapRequest,
    ControlDatabaseStatusOut,
    ControlDatabaseTestOut,
)


router = APIRouter(prefix="/api/bootstrap", tags=["bootstrap"])


def status_response() -> ControlDatabaseStatusOut:
    status = control_database_status()
    return ControlDatabaseStatusOut(
        configured=status.configured,
        initialized=status.initialized,
        source=status.source,
        database_label=status.database_label,
        error=status.error,
        bootstrap_enabled=CONTROL_DATABASE_BOOTSTRAP_ENABLED,
    )


@router.get("/status", response_model=ControlDatabaseStatusOut)
def read_control_database_status() -> ControlDatabaseStatusOut:
    return status_response()


@router.post("/test", response_model=ControlDatabaseTestOut)
def test_control_database_connection(
    payload: ControlDatabaseBootstrapRequest,
) -> ControlDatabaseTestOut:
    try:
        result = test_control_database(payload.database_url)
    except ControlDatabaseBootstrapError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"数据库连接失败：{type(error).__name__}",
        ) from error
    return ControlDatabaseTestOut.model_validate(result)


@router.post(
    "/control-database",
    response_model=ControlDatabaseStatusOut,
    status_code=201,
)
async def initialize_control_database(
    payload: ControlDatabaseBootstrapRequest,
) -> ControlDatabaseStatusOut:
    current = control_database_status()
    scheduler_was_running = current.initialized
    if scheduler_was_running:
        await auto_scan_scheduler.stop()
    try:
        await asyncio.to_thread(
            initialize_user_control_database,
            payload.database_url,
            create_database=payload.create_database,
            bootstrap_database_url=payload.bootstrap_database_url,
        )
    except ControlDatabaseAlreadyInitialized as error:
        if scheduler_was_running:
            await auto_scan_scheduler.start()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ControlDatabaseBootstrapError as error:
        if scheduler_was_running:
            await auto_scan_scheduler.start()
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        if scheduler_was_running:
            await auto_scan_scheduler.start()
        raise HTTPException(
            status_code=502,
            detail=f"控制库初始化失败：{type(error).__name__}",
        ) from error
    await auto_scan_scheduler.start()
    return status_response()
