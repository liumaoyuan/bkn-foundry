from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .file_inspector import inspect_file
from .models import Dataset, IngestionRun, PipelineTemplate, StageRun
from .storage import storage


class IngestionValidationError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def create_ingestion_run(
    db: Session,
    *,
    filename: str,
    content: bytes,
    dataset_id: str,
    template_code: str,
    source_id: str | None = None,
    space_id: str = "default",
) -> IngestionRun:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise IngestionValidationError(404, "目标数据集不存在")
    if not content:
        raise IngestionValidationError(400, "上传文件为空")
    try:
        inspection = inspect_file(filename, content)
    except Exception as error:
        raise IngestionValidationError(
            422,
            f"文件无法识别：{type(error).__name__}: {error}",
        ) from error

    template = db.scalar(
        select(PipelineTemplate).where(
            PipelineTemplate.code == template_code,
            PipelineTemplate.enabled.is_(True),
        )
    )
    if template is None:
        raise IngestionValidationError(404, "入库管线不存在或已停用")
    if inspection["file_type"] not in template.supported_types:
        labels = {
            "csv": "CSV",
            "excel": "Excel",
            "pdf": "PDF",
            "document": "DOCX",
            "image": "图片",
            "video": "视频",
        }
        supported = "、".join(labels.get(item, item) for item in template.supported_types)
        raise IngestionValidationError(
            400,
            f"{inspection['file_type']} 文件不能使用 {template.name}，"
            f"请选择 {supported} 支持的管线",
        )

    safe_filename = Path(filename).name
    run = IngestionRun(
        dataset_id=dataset_id,
        source_id=source_id,
        template_code=template_code,
        status="queued",
        original_name=safe_filename,
        file_type=inspection["file_type"],
        media_type=inspection["media_type"],
        file_size=len(content),
    )
    db.add(run)
    db.flush()
    run.source_path = storage.save_bytes(
        "raw",
        run.id,
        safe_filename,
        content,
        space_id=space_id,
    )
    for sequence, step in enumerate(template.steps, start=1):
        db.add(
            StageRun(
                run_id=run.id,
                sequence=sequence,
                stage_key=step["key"],
                name=step["name"],
                status="pending",
            )
        )
    db.commit()
    db.refresh(run)
    return run
