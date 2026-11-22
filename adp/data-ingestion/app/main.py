from __future__ import annotations

import os
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
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
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload
import httpx
from sqlalchemy import create_engine, text

from .config import APP_NAME, MAX_UPLOAD_BYTES, PROJECT_DIR
from .database import current_space_id, get_db, init_database, resolve_data_space
from .downstream_contract import (
    decode_record_cursor,
    encode_record_cursor,
    infer_dataset_schema,
)
from .file_inspector import inspect_file
from .models import (
    DataSource,
    Dataset,
    DatasetRecord,
    IngestionRun,
    PipelineTemplate,
    StageRun,
    utc_now,
)
from .pipeline import run_pipeline
from .schemas import (
    DataSourceCreate,
    DataSourceOut,
    DatasetOut,
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
from .space_api import router as space_router
from .auth import PlatformAuthMiddleware
from .config import CORS_ALLOW_ORIGINS
from .ingestion_service import IngestionValidationError, create_ingestion_run
from .data_source_service import (
    prepare_source_config,
    public_source_config,
    resolved_source_config,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_database()
    storage.ensure_ready()
    from .database import SessionLocal

    db = SessionLocal()
    try:
        seed_database(db)
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
                name=step["name"],
                status="pending",
            )
        )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "ingestion-api",
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
def agent_manifest() -> dict[str, Any]:
    return get_agent_manifest()


@app.get("/api/agent/skill")
def agent_skill() -> dict[str, Any]:
    return get_skill_content()


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
    db: Session = Depends(get_db),
) -> Dataset:
    slug = "-".join(name.lower().split()) or f"dataset-{int(datetime.now().timestamp())}"
    if db.scalar(select(Dataset.id).where(Dataset.slug == slug)):
        raise HTTPException(status_code=409, detail="同名数据集已存在")
    dataset = Dataset(
        name=name,
        slug=slug,
        description=description,
        data_type=data_type,
        target_table=slug.replace("-", "_"),
    )
    db.add(dataset)
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


@app.get("/api/datasets/{dataset_id}/schema", response_model=DatasetSchemaOut)
def dataset_schema(
    dataset_id: str,
    sample_size: Annotated[int, Query(ge=1, le=500)] = 100,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if db.get(Dataset, dataset_id) is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
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
    if db.get(Dataset, dataset_id) is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
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


@app.post("/api/sources/{source_id}/test")
def test_source(source_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    source = db.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    config = resolved_source_config(source)
    try:
        if source.source_type in {"upload", "local_dir"}:
            configured_path = str(config.get("path", "")).strip()
            if source.source_type == "local_dir" and not configured_path:
                raise ValueError("本地目录数据源需要填写目录")
            path = (
                Path(configured_path).expanduser()
                if configured_path
                else Path(PROJECT_DIR)
            )
            if not path.exists():
                raise FileNotFoundError(f"目录不存在：{path}")
        elif source.source_type in {"ftp", "sftp"}:
            host = str(config.get("host", ""))
            port = int(config.get("port") or (22 if source.source_type == "sftp" else 21))
            if not host:
                raise ValueError("缺少主机地址")
            with socket.create_connection((host, port), timeout=10):
                pass
        elif source.source_type in {"mysql", "postgres", "postgresql"}:
            if source.source_type == "mysql":
                url = "mysql+pymysql://{user}:{password}@{host}:{port}/{database}".format(
                    user=config.get("username", ""),
                    password=config.get("password", ""),
                    host=config.get("host", "127.0.0.1"),
                    port=config.get("port", 3306),
                    database=config.get("database", ""),
                )
            else:
                url = "postgresql+psycopg://{user}:{password}@{host}:{port}/{database}".format(
                    user=config.get("username", ""),
                    password=config.get("password", ""),
                    host=config.get("host", "127.0.0.1"),
                    port=config.get("port", 5432),
                    database=config.get("database", ""),
                )
            engine = create_engine(url, pool_pre_ping=True)
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            finally:
                engine.dispose()
        elif source.source_type == "api":
            url = str(config.get("url", "")).strip()
            if not url:
                raise ValueError("缺少 API URL")
            response = httpx.get(url, timeout=10)
            response.raise_for_status()
        source.status = "ready"
        source.last_checked_at = utc_now()
        db.commit()
        db.refresh(source)
        return serialize_source(source)
    except Exception as error:
        source.status = "error"
        source.last_checked_at = utc_now()
        db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"{type(error).__name__}: {error}",
        ) from error


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


@app.post("/api/uploads/inspect", response_model=InspectionOut)
async def inspect_upload(
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    filename = Path(file.filename or "unnamed").name
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过上传大小限制")
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
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过上传大小限制")

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
    new_run = IngestionRun(
        dataset_id=previous.dataset_id,
        source_id=previous.source_id,
        template_code=previous.template_code,
        status="queued",
        original_name=previous.original_name,
        file_type=previous.file_type,
        media_type=previous.media_type,
        file_size=previous.file_size,
        source_path=previous.source_path,
    )
    db.add(new_run)
    db.flush()
    add_stages(db, new_run, get_template_steps(db, new_run.template_code))
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
    if db.get(Dataset, dataset_id) is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
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
    content = storage.read_bytes(run.source_path)
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
