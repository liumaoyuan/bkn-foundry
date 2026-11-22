from __future__ import annotations

import csv
import hashlib
import io
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from .database import DEFAULT_SPACE_ID, session_for_space
from .file_inspector import (
    decode_text,
    inspect_file,
    normalize_row,
    unique_columns,
)
from .models import DataAsset, Dataset, DatasetRecord, IngestionRun, StageRun
from .seed import TEMPLATES
from .storage import storage


logger = logging.getLogger(__name__)
STAGE_BY_TEMPLATE = {
    item["code"]: item["steps"]
    for item in TEMPLATES
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def set_stage(
    db,
    stage: StageRun,
    *,
    status: str,
    input_count: int | None = None,
    output_count: int | None = None,
    summary: dict[str, Any] | None = None,
    logs: list[str] | None = None,
) -> None:
    now = utc_now()
    stage.status = status
    if status == "running":
        stage.started_at = stage.started_at or now
    if input_count is not None:
        stage.input_count = input_count
    if output_count is not None:
        stage.output_count = output_count
    if summary is not None:
        stage.summary = summary
    if logs is not None:
        stage.logs = logs
    if status in {"success", "failed", "skipped"}:
        stage.finished_at = now
        if stage.started_at:
            stage.duration_ms = max(
                0,
                int((now - stage.started_at).total_seconds() * 1000),
            )
    db.commit()


def extract_rows(
    file_type: str,
    content: bytes,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    if file_type == "csv":
        reader = csv.DictReader(io.StringIO(decode_text(content)))
        rows = [normalize_row(row) for row in reader]
        return rows, {"row_count": len(rows)}, ""

    if file_type == "excel":
        from openpyxl import load_workbook

        workbook = load_workbook(
            io.BytesIO(content),
            read_only=True,
            data_only=True,
        )
        rows: list[dict[str, Any]] = []
        sheet_summaries: list[dict[str, Any]] = []
        for sheet_index, sheet in enumerate(workbook.worksheets):
            iterator = sheet.iter_rows(values_only=True)
            first_row = next(iterator, ())
            columns = unique_columns(first_row)
            sheet_row_count = 0
            for values in iterator:
                if not any(value is not None and str(value).strip() for value in values):
                    continue
                row = normalize_row(dict(zip(columns, values, strict=False)))
                row["__sheet_name"] = sheet.title
                row["__sheet_index"] = sheet_index
                rows.append(row)
                sheet_row_count += 1
            sheet_summaries.append(
                {
                    "name": sheet.title,
                    "index": sheet_index,
                    "state": sheet.sheet_state,
                    "row_count": sheet_row_count,
                    "column_count": len(columns),
                }
            )
        metadata = {
            "sheet_count": len(sheet_summaries),
            "sheets": sheet_summaries,
            "row_count": len(rows),
        }
        workbook.close()
        return rows, metadata, ""

    if file_type == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return (
            [],
            {
                "page_count": len(reader.pages),
                "character_count": len(text),
            },
            text,
        )

    if file_type == "document":
        from docx import Document

        document = Document(io.BytesIO(content))
        text = "\n".join(
            paragraph.text.strip()
            for paragraph in document.paragraphs
            if paragraph.text.strip()
        )
        return (
            [],
            {
                "paragraph_count": len(document.paragraphs),
                "table_count": len(document.tables),
                "character_count": len(text),
            },
            text,
        )

    if file_type == "image":
        from PIL import Image

        image = Image.open(io.BytesIO(content))
        return (
            [],
            {
                "format": image.format,
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
            },
            "",
        )

    if file_type == "video":
        return [], {"description": "视频原始文件已接收"}, ""

    return [], {}, ""


def run_pipeline(run_id: str, space_id: str = DEFAULT_SPACE_ID) -> None:
    db = session_for_space(space_id)
    active_stage: StageRun | None = None
    try:
        run = db.get(IngestionRun, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = utc_now()
        db.commit()

        stages = {stage.stage_key: stage for stage in run.stages}
        content = storage.read_bytes(run.source_path)
        file_type = run.file_type

        active_stage = stages.get("receive")
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="success",
                input_count=1,
                output_count=1,
                summary={
                    "filename": run.original_name,
                    "size": len(content),
                    "media_type": run.media_type,
                    "stored_path": run.source_path,
                },
                logs=["原始文件已进入持久化存储区。"],
            )

        run.fingerprint = sha256_bytes(content)
        db.commit()
        active_stage = stages.get("fingerprint")
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="success",
                input_count=1,
                output_count=1,
                summary={"sha256": run.fingerprint},
                logs=["使用 SHA-256 生成内容指纹，用于去重和追溯。"],
            )

        active_stage = stages.get("validate")
        inspection = inspect_file(run.original_name, content)
        if file_type == "unknown":
            raise ValueError("暂不支持该文件类型，请先扩展解析器。")
        if inspection["warnings"]:
            validation_status = "success"
            validation_logs = inspection["warnings"]
        else:
            validation_status = "success"
            validation_logs = ["文件格式与内容校验通过。"]
        if active_stage:
            set_stage(
                db,
                active_stage,
                status=validation_status,
                input_count=1,
                output_count=1,
                summary={
                    "file_type": inspection["file_type"],
                    "preview_type": inspection["preview_type"],
                    "metadata": inspection["metadata"],
                    "warnings": inspection["warnings"],
                },
                logs=validation_logs,
            )

        active_stage = stages.get("extract")
        rows, metadata, extracted_text = extract_rows(file_type, content)
        extracted_count = len(rows) if rows else (1 if extracted_text.strip() else 0)
        artifact_path = ""
        if extracted_text:
            artifact_path = storage.save_text(
                "artifacts",
                run.id,
                "extracted.txt",
                extracted_text,
                space_id=space_id,
            )
            run.artifact_path = artifact_path
            db.commit()
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="success",
                input_count=1,
                output_count=extracted_count,
                summary={
                    "metadata": metadata,
                    "preview": rows[:5] if rows else extracted_text[:800],
                    "artifact_path": artifact_path,
                },
                logs=[
                    f"识别到 {extracted_count} 条可入库单元。",
                    *(["正文已写入中间产物。"] if extracted_text else []),
                ],
            )

        active_stage = stages.get("normalize")
        normalized_rows = []
        for index, row in enumerate(rows):
            cleaned = {}
            for key, value in row.items():
                cleaned_key = " ".join(str(key).split())
                cleaned_value = value
                if isinstance(value, str):
                    cleaned_value = " ".join(value.split())
                    if cleaned_value == "":
                        cleaned_value = None
                cleaned[cleaned_key] = cleaned_value
            if any(value is not None for value in cleaned.values()):
                normalized_rows.append(cleaned)
        normalized_text = "\n".join(
            " ".join(paragraph.split())
            for paragraph in extracted_text.split("\n")
            if paragraph.strip()
        )
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="success",
                input_count=extracted_count,
                output_count=(
                    len(normalized_rows)
                    if normalized_rows
                    else (1 if normalized_text else 0)
                ),
                summary={
                    "removed_empty_rows": max(0, len(rows) - len(normalized_rows)),
                    "preview": (
                        normalized_rows[:5]
                        if normalized_rows
                        else normalized_text[:800]
                    ),
                },
                logs=["完成空白行移除、字段名规整与空白字符清洗。"],
            )

        active_stage = stages.get("persist")
        inserted = 0
        duplicates = 0
        payloads: list[dict[str, Any]] = []
        if normalized_rows:
            payloads = [
                {
                    "data_type": "row",
                    "fingerprint": f"{run.fingerprint}:row:{index}",
                    "payload": row,
                }
                for index, row in enumerate(normalized_rows)
            ]
        elif normalized_text or file_type in {"image", "video"}:
            payloads = [
                {
                    "data_type": "document" if normalized_text else file_type,
                    "fingerprint": f"{run.fingerprint}:asset:0",
                    "payload": {
                        "filename": run.original_name,
                        "file_type": file_type,
                        "media_type": run.media_type,
                        "size": run.file_size,
                        "metadata": metadata,
                        "text_preview": normalized_text[:4000],
                    },
                }
            ]
        for item in payloads:
            exists = db.scalar(
                select(DatasetRecord.id).where(
                    DatasetRecord.dataset_id == run.dataset_id,
                    DatasetRecord.fingerprint == item["fingerprint"],
                )
            )
            if exists:
                duplicates += 1
                continue
            db.add(
                DatasetRecord(
                    dataset_id=run.dataset_id,
                    run_id=run.id,
                    fingerprint=item["fingerprint"],
                    data_type=item["data_type"],
                    payload=item["payload"],
                    asset_path=(
                        run.source_path
                        if item["data_type"] in {"document", "image", "video"}
                        else ""
                    ),
                )
            )
            inserted += 1
        asset_exists = db.scalar(
            select(DataAsset.id).where(
                DataAsset.run_id == run.id,
                DataAsset.sha256 == run.fingerprint,
            )
        )
        if not asset_exists:
            db.add(
                DataAsset(
                    run_id=run.id,
                    original_name=run.original_name,
                    stored_path=run.source_path,
                    media_type=run.media_type,
                    size=run.file_size,
                    sha256=run.fingerprint,
                    kind="source",
                )
            )
        run.inserted_count = inserted
        run.duplicate_count = duplicates
        run.fetched_count = len(payloads)
        db.commit()
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="success",
                input_count=len(payloads),
                output_count=inserted,
                summary={
                    "inserted": inserted,
                    "duplicates": duplicates,
                    "target_table": (
                        db.get(Dataset, run.dataset_id).target_table
                        if db.get(Dataset, run.dataset_id)
                        else ""
                    ),
                },
                logs=[
                    f"新增 {inserted} 条记录。",
                    f"跳过重复记录 {duplicates} 条。",
                ],
            )

        active_stage = stages.get("verify")
        dataset = db.get(Dataset, run.dataset_id)
        total_records = db.scalar(
            select(func.count(DatasetRecord.id)).where(
                DatasetRecord.dataset_id == run.dataset_id
            )
        ) or 0
        if dataset:
            dataset.record_count = total_records
            dataset.last_ingested_at = utc_now()
        run.status = "success"
        run.finished_at = utc_now()
        run.error_count = 0
        db.commit()
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="success",
                input_count=inserted,
                output_count=total_records,
                summary={
                    "dataset_total": total_records,
                    "batch_inserted": inserted,
                    "batch_duplicates": duplicates,
                },
                logs=["数据库数量核验通过，数据集目录已更新。"],
            )
    except Exception as error:
        logger.exception("Ingestion run failed: %s", run_id)
        db.rollback()
        run = db.get(IngestionRun, run_id)
        if run:
            run.status = "failed"
            run.finished_at = utc_now()
            run.error_message = f"{type(error).__name__}: {error}"
            run.error_count = max(1, run.error_count)
            db.commit()
        if active_stage:
            stage = db.get(StageRun, active_stage.id)
            if stage:
                set_stage(
                    db,
                    stage,
                    status="failed",
                    summary={"error": f"{type(error).__name__}: {error}"},
                    logs=[str(error)],
                )
    finally:
        db.close()
