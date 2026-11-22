from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .auto_scan import auto_scan_scheduler, next_scan_at
from .config import AUTO_SCAN_ENABLED
from .database import current_space_id, get_db, session_for_space
from .models import AutoScanJob, DataSource, Dataset, utc_now
from .schemas import AutoScanJobCreate, AutoScanJobUpdate


router = APIRouter(prefix="/api/auto-scan", tags=["auto-scan"])


def serialize_job(job: AutoScanJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "name": job.name,
        "source_id": job.source_id,
        "source_name": job.source.name if job.source else "",
        "source_type": job.source.source_type if job.source else "",
        "dataset_id": job.dataset_id,
        "dataset_name": job.dataset.name if job.dataset else "",
        "template_code": job.template_code,
        "scan_type": job.scan_type,
        "path": job.path,
        "recursive": job.recursive,
        "file_pattern": job.file_pattern,
        "interval_seconds": job.interval_seconds,
        "schedule_type": job.schedule_type,
        "schedule_time": job.schedule_time,
        "weekdays": job.weekdays,
        "cron_expression": job.cron_expression,
        "timezone": job.timezone,
        "enabled": job.enabled,
        "status": job.status,
        "last_scan_at": job.last_scan_at.isoformat() if job.last_scan_at else None,
        "next_scan_at": job.next_scan_at.isoformat() if job.next_scan_at else None,
        "last_error": job.last_error,
        "last_run_id": job.last_run_id,
        "processed_count": job.processed_count,
        "scan_state": job.scan_state,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def load_job(db: Session, job_id: str) -> AutoScanJob:
    job = db.scalar(
        select(AutoScanJob)
        .where(AutoScanJob.id == job_id)
        .options(
            selectinload(AutoScanJob.source),
            selectinload(AutoScanJob.dataset),
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="扫描任务不存在")
    return job


def validate_scan_type(source: DataSource, scan_type: str) -> None:
    accepted = {
        "local": {"upload", "local_dir"},
        "ftp": {"ftp"},
        "sftp": {"sftp"},
        "database": {"mysql", "postgres", "postgresql"},
    }
    if scan_type not in accepted:
        raise HTTPException(status_code=400, detail="不支持的扫描类型")
    if source.source_type not in accepted[scan_type]:
        raise HTTPException(
            status_code=400,
            detail=f"{source.source_type} 数据源不能用于 {scan_type} 扫描",
        )


@router.get("/status")
def scheduler_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    enabled = db.scalar(
        select(AutoScanJob.id).where(AutoScanJob.enabled.is_(True))
    )
    return {
        "scheduler_enabled": AUTO_SCAN_ENABLED,
        "running_job": bool(
            db.scalar(
                select(AutoScanJob.id).where(AutoScanJob.status == "running")
            )
        ),
        "has_enabled_jobs": enabled is not None,
    }


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    jobs = db.scalars(
        select(AutoScanJob)
        .options(
            selectinload(AutoScanJob.source),
            selectinload(AutoScanJob.dataset),
        )
        .order_by(AutoScanJob.created_at.desc())
    ).all()
    return [serialize_job(job) for job in jobs]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return serialize_job(load_job(db, job_id))


@router.post("/jobs")
def create_job(
    payload: AutoScanJobCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    source = db.get(DataSource, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    if db.get(Dataset, payload.dataset_id) is None:
        raise HTTPException(status_code=404, detail="目标数据集不存在")
    validate_scan_type(source, payload.scan_type)
    if payload.scan_type == "local" and not (payload.path or source.config.get("path")):
        raise HTTPException(status_code=400, detail="本地扫描需要填写目录")
    job = AutoScanJob(
        name=payload.name,
        source_id=payload.source_id,
        dataset_id=payload.dataset_id,
        template_code=payload.template_code,
        scan_type=payload.scan_type,
        path=payload.path,
        recursive=payload.recursive,
        file_pattern=payload.file_pattern,
        interval_seconds=payload.interval_seconds,
        schedule_type=payload.schedule_type,
        schedule_time=payload.schedule_time,
        weekdays=payload.weekdays,
        cron_expression=payload.cron_expression,
        timezone=payload.timezone,
        enabled=payload.enabled,
        status="ready",
        next_scan_at=None,
    )
    if payload.enabled:
        try:
            job.next_scan_at = next_scan_at(job)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    db.add(job)
    db.commit()
    db.refresh(job)
    return serialize_job(load_job(db, job.id))


@router.put("/jobs/{job_id}")
def update_job(
    job_id: str,
    payload: AutoScanJobUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    job = load_job(db, job_id)
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(job, key, value)
    if "enabled" in values:
        next_scan = utc_now()
        if not job.enabled:
            next_scan = None
        job.next_scan_at = next_scan
    if "interval_seconds" in values and job.enabled:
        job.next_scan_at = next_scan_at(job)
    if any(
        key in values
        for key in (
            "schedule_type",
            "schedule_time",
            "weekdays",
            "cron_expression",
            "timezone",
        )
    ) and job.enabled:
        try:
            job.next_scan_at = next_scan_at(job)
        except ValueError as error:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(error)) from error
    db.commit()
    return serialize_job(load_job(db, job.id))


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    job = db.get(AutoScanJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="扫描任务不存在")
    db.delete(job)
    db.commit()
    return {"deleted": True}


@router.post("/jobs/{job_id}/run")
async def run_job_now(job_id: str, request: Request) -> dict[str, Any]:
    space_id = current_space_id(request)
    try:
        await auto_scan_scheduler.run_job_now(job_id, space_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    db = session_for_space(space_id)
    try:
        return serialize_job(load_job(db, job_id))
    finally:
        db.close()
