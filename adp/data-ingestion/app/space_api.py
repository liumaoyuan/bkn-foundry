# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .catalog_sync import CatalogSyncError, sync_space_catalog
from .database import (
    SessionLocal,
    database_label,
    initialize_space_database,
    invalidate_space_cache,
    list_data_spaces,
    require_initialized_control_database,
    validate_database_url,
)
from .models import DataSpace, new_id
from .schemas import DataSpaceCreate, DataSpaceOut
from .secret_store import encrypt_secret


router = APIRouter(prefix="/api/spaces", tags=["data-spaces"])


@router.get(
    "",
    response_model=list[DataSpaceOut],
    dependencies=[Depends(require_initialized_control_database)],
)
def read_data_spaces() -> list[DataSpace]:
    return list_data_spaces()


@router.post(
    "",
    response_model=DataSpaceOut,
    dependencies=[Depends(require_initialized_control_database)],
)
def create_data_space(payload: DataSpaceCreate) -> DataSpace:
    try:
        database_url = validate_database_url(payload.database_url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    db = SessionLocal()
    space_id = new_id()
    try:
        duplicate = db.scalar(
            select(DataSpace.id).where(DataSpace.slug == payload.slug)
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="数据空间标识已存在")
        space = DataSpace(
            id=space_id,
            name=payload.name.strip(),
            slug=payload.slug.strip().lower(),
            database_url_encrypted=encrypt_secret(database_url),
            database_label=database_label(database_url),
            is_default=False,
            status="initializing",
        )
        db.add(space)
        db.commit()
        try:
            configured_engine = initialize_space_database(space)
            with configured_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as error:
            space = db.get(DataSpace, space_id)
            if space is not None:
                space.status = "error"
                space.last_error = f"{type(error).__name__}: 数据库初始化失败"
                db.commit()
            raise HTTPException(
                status_code=503,
                detail="目标数据库连接或初始化失败",
            ) from error
        space = db.get(DataSpace, space_id)
        if space is None:
            raise HTTPException(status_code=500, detail="数据空间保存失败")
        space.status = "ready"
        space.last_error = ""
        db.commit()
        db.refresh(space)
        return space
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        invalidate_space_cache(space_id)
        raise HTTPException(status_code=500, detail="数据空间创建失败") from error
    finally:
        db.close()


@router.post(
    "/{space_id}/test",
    dependencies=[Depends(require_initialized_control_database)],
)
def test_data_space(space_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        space = db.get(DataSpace, space_id)
        if space is None:
            raise HTTPException(status_code=404, detail="数据空间不存在")
        try:
            configured_engine = initialize_space_database(space)
            with configured_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as error:
            space.status = "error"
            space.last_error = f"{type(error).__name__}: 数据库不可用"
            db.commit()
            raise HTTPException(
                status_code=503,
                detail="目标数据库连接失败",
            ) from error
        space.status = "ready"
        space.last_error = ""
        db.commit()
        return {
            "id": space.id,
            "status": space.status,
            "database_label": space.database_label,
        }
    finally:
        db.close()


@router.post(
    "/{space_id}/catalog-sync",
    dependencies=[Depends(require_initialized_control_database)],
)
async def sync_data_space_catalog(
    space_id: str,
    request: Request,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        space = db.get(DataSpace, space_id)
        if space is None:
            raise HTTPException(status_code=404, detail="数据空间不存在")
        caller = getattr(request.state, "caller", None)
        access_token = getattr(caller, "access_token", "") if caller else ""
        if not access_token:
            authorization = request.headers.get("authorization", "")
            if authorization.startswith("Bearer "):
                access_token = authorization[7:].strip()
        try:
            return await sync_space_catalog(
                space,
                access_token=access_token,
            )
        except CatalogSyncError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
    finally:
        db.close()
