from __future__ import annotations

import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from .config import APP_NAME, MAX_UPLOAD_BYTES
from .database import (
    ControlDatabaseNotInitialized,
    control_database_status,
    current_space_id,
    get_db,
    init_database,
    resolve_data_space,
)
from .downstream_contract import (
    decode_record_cursor,
    encode_record_cursor,
    infer_dataset_schema,
)
from .file_inspector import inspect_file
from .models import (
    AutoScanJob,
    DataAsset,
    DataSource,
    Dataset,
    DatasetRecord,
    IngestionRun,
    PipelineTemplate,
    StageRun,
    utc_now,
)
from .pipeline import run_pipeline
from .record_storage import (
    backfill_dedicated_record_keys,
    business_record_key,
    count_dedicated_records,
    delete_dedicated_run,
    drop_dedicated_table,
    page_dedicated_records,
    sample_dedicated_records,
)
from .schemas import (
    DataSourceCreate,
    DataSourceOut,
    DataSourceUpdate,
    DatasetOut,
    DatasetUpdate,
    DatasetRecordOut,
    DatasetRecordChangesOut,
    DatasetSchemaOut,
    InspectionOut,
    RunListItem,
    RunOut,
    TemplateOut,
)
from .seed import TEMPLATES, seed_database
from .storage import storage
from .agent_catalog import get_agent_manifest, get_skill_content
from .agent_api import router as agent_router
from .auto_scan import auto_scan_scheduler
from .auto_scan_api import router as auto_scan_router
from .bootstrap_api import router as bootstrap_router
from .capability_api import router as capability_router
from .space_api import router as space_router
from .auth import PlatformAuthMiddleware
from .config import CORS_ALLOW_ORIGINS
from .ingestion_service import IngestionValidationError, create_ingestion_run
from .data_source_service import (
    prepare_source_config,
    prepare_source_config_update,
    public_source_config,
    resolved_source_config,
)
from .source_connectors import test_source_connection

logger = logging.getLogger(__name__)


async def read_upload_bytes(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过上传大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.ensure_ready()
    initialized = False
    try:
        init_database()
        initialized = True
    except ControlDatabaseNotInitialized:
        logger.warning(
            "Data-ingestion control database is not initialized; "
            "waiting for an administrator to configure it in Studio."
        )
    if initialized:
        from .database import SessionLocal

        db = SessionLocal()
        try:
            seed_database(db, include_examples=False)
        finally:
            db.close()
        await auto_scan_scheduler.start()
    try:
        yield
    finally:
        await auto_scan_scheduler.stop()


app = FastAPI(
    title=APP_NAME,
    description="多源数据接入、清洗、入库和过程追踪 API",
    version="0.1.0",
    lifespan=lifespan,
)

if CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept-Language", "x-trace-id"],
    )
app.add_middleware(PlatformAuthMiddleware)
app.include_router(bootstrap_router)
app.include_router(capability_router)
app.include_router(agent_router)
app.include_router(auto_scan_router)
app.include_router(space_router)


def serialize_run(run: IngestionRun) -> dict[str, Any]:
    return RunOut.model_validate(run).model_dump(mode="json")


def template_types_text(types: list[str]) -> str:
    labels = {
        "csv": "CSV",
        "excel": "Excel",
        "pdf": "PDF",
        "document": "DOCX",
        "image": "图片",
        "video": "视频",
    }
    return "、".join(labels.get(item, item) for item in types)


def get_template_steps(db: Session, template_code: str) -> list[dict[str, str]]:
    template = db.scalar(
        select(PipelineTemplate).where(PipelineTemplate.code == template_code)
    )
    if template:
        return template.steps
    fallback = next(
        (item for item in TEMPLATES if item["code"] == template_code),
        TEMPLATES[0],
    )
    return fallback["steps"]


def add_stages(
    db: Session,
    run: IngestionRun,
    steps: list[dict[str, Any]],
) -> None:
    for sequence, step in enumerate(steps, start=1):
        db.add(
            StageRun(
                run_id=run.id,
                sequence=sequence,
                stage_key=step["key"],
                name=(
                    str(step.get("name") or "").strip()
                    or str(step.get("tool_name") or "").strip()
                    or step["key"]
                ),
                status="pending",
            )
        )


@app.get("/api/health")
def health() -> dict[str, str]:
    status = control_database_status()
    return {
        "status": "ok",
        "service": "ingestion-api",
        "control_database": "ready" if status.initialized else "uninitialized",
        "time": datetime.now(UTC).isoformat(),
    }


@app.get("/api/storage/info")
def storage_info(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    info = storage.info()
    space = resolve_data_space(current_space_id(request))
    return {
        **info,
        "space_id": space.id,
        "space_name": space.name,
        "database_backend": space.database_label.split(":", 1)[0],
        "database_label": space.database_label,
        "raw_storage": "raw/<run_id>/<filename>",
        "artifact_storage": "artifacts/<run_id>/",
    }


@app.get("/api/agent/manifest")
def agent_manifest(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_agent_manifest(db)


@app.get("/api/agent/skill")
def agent_skill(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_skill_content(db)


@app.get("/api/overview")
def overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    dataset_count = db.scalar(select(func.count(Dataset.id))) or 0
    record_count = db.scalar(select(func.count(DatasetRecord.id))) or 0
    run_count = db.scalar(select(func.count(IngestionRun.id))) or 0
    success_count = db.scalar(
        select(func.count(IngestionRun.id)).where(
            IngestionRun.status == "success"
        )
    ) or 0
    active_count = db.scalar(
        select(func.count(IngestionRun.id)).where(
            IngestionRun.status.in_(["queued", "running"])
        )
    ) or 0
    recent = db.scalars(
        select(IngestionRun)
        .order_by(IngestionRun.created_at.desc())
        .limit(6)
    ).all()
    return {
        "datasets": dataset_count,
        "records": record_count,
        "runs": run_count,
        "success_runs": success_count,
        "active_runs": active_count,
        "success_rate": round((success_count / run_count) * 100, 1)
        if run_count
        else 0,
        "recent_runs": [
            RunListItem.model_validate(item).model_dump(mode="json")
            for item in recent
        ],
    }


@app.get("/api/datasets", response_model=list[DatasetOut])
def datasets(
    q: Annotated[str | None, Query()] = None,
    db: Session = Depends(get_db),
) -> list[Dataset]:
    statement = select(Dataset).order_by(Dataset.created_at.asc())
    if q:
        like = f"%{q}%"
        statement = statement.where(
            or_(Dataset.name.like(like), Dataset.description.like(like))
        )
    return list(db.scalars(statement).all())


@app.post("/api/datasets", response_model=DatasetOut)
def create_dataset(
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    data_type: Annotated[str, Form()] = "mixed",
    storage_mode: Annotated[str, Form()] = "auto",
    upsert_keys: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Dataset:
    storage_mode = storage_mode.strip().lower()
    if storage_mode not in {"auto", "shared", "dedicated"}:
        raise HTTPException(status_code=400, detail="数据集存储模式无效")
    slug = "-".join(name.lower().split()) or f"dataset-{int(datetime.now().timestamp())}"
    if db.scalar(select(Dataset.id).where(Dataset.slug == slug)):
        raise HTTPException(status_code=409, detail="同名数据集已存在")
    dataset = Dataset(
        name=name,
        slug=slug,
        description=description,
        data_type=data_type,
        target_table=slug.replace("-", "_"),
        storage_mode=storage_mode,
        upsert_keys=[
            item.strip()
            for item in upsert_keys.split(",")
            if item.strip()
        ],
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    if storage_mode == "dedicated":
        from .record_storage import ensure_dataset_table

        try:
            dataset.physical_table = ensure_dataset_table(
                db.get_bind(),
                dataset,
            )
            dataset.partition_mode = (
                "month"
                if db.get_bind().dialect.name in {"mysql", "mariadb"}
                else ""
            )
            db.commit()
            db.refresh(dataset)
        except Exception as error:
            db.delete(dataset)
            db.commit()
            raise HTTPException(
                status_code=503,
                detail="独立数据表创建失败",
            ) from error
    return dataset


@app.put("/api/datasets/{dataset_id}", response_model=DatasetOut)
def update_dataset(
    dataset_id: str,
    payload: DatasetUpdate,
    db: Session = Depends(get_db),
) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    values = payload.model_dump(exclude_unset=True)
    if "name" in values and values["name"] is not None:
        name = str(values["name"]).strip()
        slug = "-".join(name.lower().split()) or dataset.slug
        duplicate = db.scalar(
            select(Dataset.id).where(
                Dataset.slug == slug,
                Dataset.id != dataset.id,
            )
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="同名数据集已存在")
        dataset.name = name
        dataset.slug = slug
    if "description" in values and values["description"] is not None:
        dataset.description = str(values["description"])
    if "data_type" in values and values["data_type"] is not None:
        dataset.data_type = str(values["data_type"])
    if "upsert_keys" in values and values["upsert_keys"] is not None:
        dataset.upsert_keys = [
            str(item).strip()
            for item in values["upsert_keys"]
            if str(item).strip()
        ]
        if dataset.upsert_keys:
            if dataset.physical_table:
                backfill_dedicated_record_keys(
                    db.get_bind(),
                    dataset.physical_table,
                    dataset.upsert_keys,
                )
            else:
                for record in db.scalars(
                    select(DatasetRecord).where(
                        DatasetRecord.dataset_id == dataset.id
                    )
                ).all():
                    record.record_key = business_record_key(
                        dataset.upsert_keys,
                        record.payload or {},
                    )
    db.commit()
    db.refresh(dataset)
    return dataset


@app.get("/api/datasets/{dataset_id}", response_model=DatasetOut)
def dataset_detail(
    dataset_id: str,
    db: Session = Depends(get_db),
) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return dataset


@app.delete("/api/datasets/{dataset_id}", status_code=204)
def delete_dataset(
    dataset_id: str,
    db: Session = Depends(get_db),
) -> Response:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    physical_table = dataset.physical_table
    active = db.scalar(
        select(IngestionRun.id).where(
            IngestionRun.dataset_id == dataset_id,
            IngestionRun.status.in_(["queued", "running"]),
        )
    )
    if active:
        raise HTTPException(status_code=409, detail="数据集仍有运行中的入库任务")
    run_ids = list(
        db.scalars(select(IngestionRun.id).where(IngestionRun.dataset_id == dataset_id))
    )
    stored_paths: set[str] = set()
    if run_ids:
        for source_path, artifact_path in db.execute(
            select(IngestionRun.source_path, IngestionRun.artifact_path).where(
                IngestionRun.id.in_(run_ids)
            )
        ):
            if source_path:
                stored_paths.add(source_path)
            if artifact_path:
                stored_paths.add(artifact_path)
        stored_paths.update(
            path
            for path in db.scalars(
                select(DataAsset.stored_path).where(DataAsset.run_id.in_(run_ids))
            ).all()
            if path
        )
    try:
        db.query(AutoScanJob).filter(AutoScanJob.dataset_id == dataset_id).delete(
            synchronize_session=False
        )
        if run_ids:
            db.query(DataAsset).filter(DataAsset.run_id.in_(run_ids)).delete(
                synchronize_session=False
            )
            db.query(StageRun).filter(StageRun.run_id.in_(run_ids)).delete(
                synchronize_session=False
            )
        db.query(DatasetRecord).filter(DatasetRecord.dataset_id == dataset_id).delete(
            synchronize_session=False
        )
        if run_ids:
            db.query(IngestionRun).filter(IngestionRun.id.in_(run_ids)).delete(
                synchronize_session=False
            )
        db.delete(dataset)
        db.commit()
        for stored_path in stored_paths:
            try:
                storage.delete(stored_path)
            except Exception:
                logger.warning("Failed to delete dataset file: %s", stored_path)
        if physical_table:
            drop_dedicated_table(db.get_bind(), physical_table)
    except Exception as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="数据集删除失败，仍有引用") from error
    return Response(status_code=204)


@app.get("/api/datasets/{dataset_id}/schema", response_model=DatasetSchemaOut)
def dataset_schema(
    dataset_id: str,
    sample_size: Annotated[int, Query(ge=1, le=500)] = 100,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if dataset.physical_table:
        total = count_dedicated_records(db.get_bind(), dataset.physical_table)
        records = [
            SimpleNamespace(payload=item["payload"])
            for item in sample_dedicated_records(
                db.get_bind(),
                dataset.physical_table,
                limit=sample_size,
            )
        ]
    else:
        total = db.scalar(
            select(func.count(DatasetRecord.id)).where(
                DatasetRecord.dataset_id == dataset_id
            )
        ) or 0
        records = db.scalars(
            select(DatasetRecord)
            .where(DatasetRecord.dataset_id == dataset_id)
            .order_by(DatasetRecord.created_at.desc(), DatasetRecord.id.desc())
            .limit(sample_size)
        ).all()
    return infer_dataset_schema(records, total_records=total)


@app.get(
    "/api/datasets/{dataset_id}/records/changes",
    response_model=DatasetRecordChangesOut,
)
def dataset_record_changes(
    dataset_id: str,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if dataset.physical_table:
        clauses = ["1=1"]
        params: dict[str, Any] = {"limit": limit + 1}
        if cursor:
            try:
                position = decode_record_cursor(cursor, dataset_id)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            clauses.append(
                "(created_at > :cursor_time OR "
                "(created_at = :cursor_time AND id > :cursor_id))"
            )
            params.update(
                {
                    "cursor_time": position.created_at.replace(tzinfo=None),
                    "cursor_id": position.record_id,
                }
            )
        with db.get_bind().connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, run_id, fingerprint, data_type, asset_path, "
                    "payload, created_at "
                    f"FROM `{dataset.physical_table}` "
                    f"WHERE {' AND '.join(clauses)} "
                    "ORDER BY created_at ASC, id ASC LIMIT :limit"
                ),
                params,
            ).mappings().all()
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        visible = [
            {
                "id": row["id"],
                "dataset_id": dataset_id,
                "run_id": row["run_id"],
                "data_type": row["data_type"],
                "payload": (
                    json.loads(row["payload"])
                    if isinstance(row["payload"], str)
                    else row["payload"]
                ),
                "asset_path": row["asset_path"],
                "created_at": row["created_at"],
            }
            for row in visible_rows
        ]
        next_cursor = None
        if visible_rows:
            last = visible_rows[-1]
            next_cursor = encode_record_cursor(
                DatasetRecord(
                    id=last["id"],
                    dataset_id=dataset_id,
                    created_at=last["created_at"],
                )
            )
        return {
            "items": visible,
            "next_cursor": next_cursor or cursor,
            "has_more": has_more,
        }
    statement = select(DatasetRecord).where(
        DatasetRecord.dataset_id == dataset_id
    )
    if cursor:
        try:
            position = decode_record_cursor(cursor, dataset_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        statement = statement.where(
            or_(
                DatasetRecord.created_at > position.created_at,
                and_(
                    DatasetRecord.created_at == position.created_at,
                    DatasetRecord.id > position.record_id,
                ),
            )
        )
    records = db.scalars(
        statement.order_by(
            DatasetRecord.created_at.asc(),
            DatasetRecord.id.asc(),
        ).limit(limit + 1)
    ).all()
    has_more = len(records) > limit
    visible = records[:limit]
    return {
        "items": [
            DatasetRecordOut.model_validate(item).model_dump(mode="json")
            for item in visible
        ],
        "next_cursor": (
            encode_record_cursor(visible[-1])
            if visible
            else cursor
        ),
        "has_more": has_more,
    }


@app.get("/api/templates", response_model=list[TemplateOut])
def templates(db: Session = Depends(get_db)) -> list[PipelineTemplate]:
    return list(
        db.scalars(
            select(PipelineTemplate)
            .where(PipelineTemplate.enabled.is_(True))
            .order_by(PipelineTemplate.created_at.asc())
        ).all()
    )


@app.get("/api/templates/{template_code}", response_model=TemplateOut)
def template_detail(
    template_code: str,
    db: Session = Depends(get_db),
) -> PipelineTemplate:
    template = db.scalar(
        select(PipelineTemplate).where(PipelineTemplate.code == template_code)
    )
    if template is None:
        raise HTTPException(status_code=404, detail="入库管线不存在")
    return template


def serialize_source(source: DataSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "name": source.name,
        "source_type": source.source_type,
        "config": public_source_config(source.config),
        "status": source.status,
        "last_checked_at": (
            source.last_checked_at.isoformat() if source.last_checked_at else None
        ),
        "created_at": source.created_at.isoformat(),
    }


@app.get("/api/sources")
def sources(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    items = db.scalars(
        select(DataSource).order_by(DataSource.created_at.asc())
    ).all()
    return [serialize_source(item) for item in items]


@app.post("/api/sources")
def create_source(
    payload: DataSourceCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    source = DataSource(
        name=payload.name,
        source_type=payload.source_type,
        config=prepare_source_config(payload.config),
        status="ready" if payload.source_type == "upload" else "configured",
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return serialize_source(source)


@app.put("/api/sources/{source_id}")
def update_source(
    source_id: str,
    payload: DataSourceUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    source = db.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    values = payload.model_dump(exclude_unset=True)
    if "name" in values and values["name"] is not None:
        name = str(values["name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="数据源名称不能为空")
        source.name = name
    if "source_type" in values and values["source_type"] is not None:
        source.source_type = str(values["source_type"])
    if "config" in values and values["config"] is not None:
        source.config = prepare_source_config_update(
            source.config or {},
            values["config"],
        )
    source.status = "ready" if source.source_type == "upload" else "configured"
    source.last_checked_at = None
    db.commit()
    db.refresh(source)
    return serialize_source(source)


@app.post("/api/sources/{source_id}/test")
def test_source(source_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    source = db.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    config = resolved_source_config(source)
    try:
        details = test_source_connection(source.source_type, config)
        source.status = "ready"
        source.last_checked_at = utc_now()
        db.commit()
        db.refresh(source)
        return {
            **serialize_source(source),
            "test_result": details,
        }
    except Exception as error:
        source.status = "error"
        source.last_checked_at = utc_now()
        db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"{type(error).__name__}: {error}",
        ) from error


@app.delete("/api/sources/{source_id}", status_code=204)
def delete_source(source_id: str, db: Session = Depends(get_db)) -> Response:
    source = db.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    try:
        db.query(AutoScanJob).filter(AutoScanJob.source_id == source_id).delete(
            synchronize_session=False
        )
        db.query(IngestionRun).filter(IngestionRun.source_id == source_id).update(
            {IngestionRun.source_id: None},
            synchronize_session=False,
        )
        db.delete(source)
        db.commit()
    except Exception as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="数据源删除失败，仍有引用") from error
    return Response(status_code=204)


@app.get("/api/runs", response_model=list[RunListItem])
def runs(
    status: Annotated[str | None, Query()] = None,
    dataset_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    db: Session = Depends(get_db),
) -> list[IngestionRun]:
    statement = select(IngestionRun)
    if status:
        statement = statement.where(IngestionRun.status == status)
    if dataset_id:
        statement = statement.where(IngestionRun.dataset_id == dataset_id)
    statement = statement.order_by(IngestionRun.created_at.desc()).limit(limit)
    return list(db.scalars(statement).all())


@app.get("/api/runs/page")
def runs_page(
    status: Annotated[str | None, Query()] = None,
    dataset_id: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 100,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    filters = []
    if status:
        filters.append(IngestionRun.status == status)
    if dataset_id:
        filters.append(IngestionRun.dataset_id == dataset_id)
    total = db.scalar(
        select(func.count(IngestionRun.id)).where(*filters)
    ) or 0
    items = db.scalars(
        select(IngestionRun)
        .where(*filters)
        .order_by(IngestionRun.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            RunListItem.model_validate(item).model_dump(mode="json")
            for item in items
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.scalar(
        select(IngestionRun)
        .where(IngestionRun.id == run_id)
        .options(selectinload(IngestionRun.stages))
    )
    if run is None:
        raise HTTPException(status_code=404, detail="入库任务不存在")
    return serialize_run(run)


@app.delete("/api/runs/{run_id}", status_code=204)
def delete_run(run_id: str, db: Session = Depends(get_db)) -> Response:
    run = db.get(IngestionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="入库任务不存在")
    if run.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="运行中的入库任务不能删除")
    source_path = run.source_path
    artifact_path = run.artifact_path
    dataset_id = run.dataset_id
    dataset = db.get(Dataset, dataset_id)
    try:
        db.query(DataAsset).filter(DataAsset.run_id == run_id).delete(
            synchronize_session=False
        )
        db.query(StageRun).filter(StageRun.run_id == run_id).delete(
            synchronize_session=False
        )
        if dataset is not None and dataset.physical_table:
            delete_dedicated_run(
                db.get_bind(),
                dataset.physical_table,
                run_id,
            )
        else:
            db.query(DatasetRecord).filter(DatasetRecord.run_id == run_id).delete(
                synchronize_session=False
            )
        db.delete(run)
        db.flush()
        if dataset is not None:
            dataset.record_count = (
                count_dedicated_records(db.get_bind(), dataset.physical_table)
                if dataset.physical_table
                else (
                    db.scalar(
                        select(func.count(DatasetRecord.id)).where(
                            DatasetRecord.dataset_id == dataset_id
                        )
                    )
                    or 0
                )
            )
        db.commit()
        for uri in {source_path, artifact_path}:
            if uri:
                try:
                    storage.delete(uri)
                except Exception:
                    logger.warning("Failed to delete stored object: %s", uri)
    except Exception as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="入库任务删除失败") from error
    return Response(status_code=204)


@app.post("/api/uploads/inspect", response_model=InspectionOut)
async def inspect_upload(
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    filename = Path(file.filename or "unnamed").name
    content = await read_upload_bytes(file)
    try:
        return inspect_file(filename, content)
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"文件预览失败：{type(error).__name__}: {error}",
        ) from error


@app.post("/api/runs")
async def create_run(
    background_tasks: BackgroundTasks,
    request: Request,
    file: Annotated[UploadFile, File()],
    dataset_id: Annotated[str, Form()],
    template_code: Annotated[str, Form()] = "tabular_standard",
    source_id: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    filename = Path(file.filename or "unnamed").name
    content = await read_upload_bytes(file)

    try:
        run = create_ingestion_run(
            db,
            filename=filename,
            content=content,
            dataset_id=dataset_id,
            template_code=template_code,
            source_id=source_id,
            space_id=current_space_id(request),
        )
    except IngestionValidationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    background_tasks.add_task(run_pipeline, run.id, current_space_id(request))
    return serialize_run(run)


@app.post("/api/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    previous = db.get(IngestionRun, run_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="原任务不存在")
    if not previous.source_path or not storage.exists(previous.source_path):
        raise HTTPException(status_code=409, detail="原始文件已丢失，无法重试")
    template = db.scalar(
        select(PipelineTemplate).where(
            PipelineTemplate.code == previous.template_code,
            PipelineTemplate.enabled.is_(True),
        )
    )
    if template is None:
        raise HTTPException(status_code=409, detail="原任务使用的管线已删除或停用")
    content = storage.read_bytes(previous.source_path)
    new_run = IngestionRun(
        dataset_id=previous.dataset_id,
        source_id=previous.source_id,
        template_code=previous.template_code,
        status="queued",
        original_name=previous.original_name,
        file_type=previous.file_type,
        media_type=previous.media_type,
        file_size=previous.file_size,
    )
    db.add(new_run)
    db.flush()
    new_run.source_path = storage.save_bytes(
        "raw",
        new_run.id,
        previous.original_name,
        content,
        space_id=current_space_id(request),
    )
    add_stages(db, new_run, template.steps)
    db.commit()
    background_tasks.add_task(run_pipeline, new_run.id, current_space_id(request))
    db.refresh(new_run)
    return serialize_run(new_run)


@app.get("/api/datasets/{dataset_id}/records")
def dataset_records(
    dataset_id: str,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    q: Annotated[str | None, Query()] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if dataset.physical_table:
        total, items = page_dedicated_records(
            db.get_bind(),
            dataset.physical_table,
            page=page,
            page_size=page_size,
            query=q or "",
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    **item,
                    "dataset_id": dataset_id,
                }
                for item in items
            ],
        }
    statement = select(DatasetRecord).where(
        DatasetRecord.dataset_id == dataset_id
    )
    count_statement = select(func.count(DatasetRecord.id)).where(
        DatasetRecord.dataset_id == dataset_id
    )
    if q:
        like = f"%{q}%"
        statement = statement.where(DatasetRecord.payload.cast(str).like(like))
        count_statement = count_statement.where(
            DatasetRecord.payload.cast(str).like(like)
        )
    total = db.scalar(count_statement) or 0
    records = db.scalars(
        statement.order_by(DatasetRecord.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            DatasetRecordOut.model_validate(item).model_dump(mode="json")
            for item in records
        ],
    }


@app.get("/api/runs/{run_id}/asset")
def download_source_asset(run_id: str, db: Session = Depends(get_db)) -> Response:
    run = db.get(IngestionRun, run_id)
    if run is None or not run.source_path:
        raise HTTPException(status_code=404, detail="原始文件不存在")
    if not storage.exists(run.source_path):
        raise HTTPException(status_code=404, detail="原始文件已丢失")
    try:
        content = storage.read_bytes(run.source_path)
    except Exception as error:
        raise HTTPException(status_code=404, detail="原始文件已丢失") from error
    if not content:
        raise HTTPException(status_code=404, detail="原始文件已丢失")
    encoded = quote(run.original_name)
    return Response(
        content=content,
        media_type=run.media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        },
    )
