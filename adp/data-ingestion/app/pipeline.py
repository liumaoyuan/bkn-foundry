from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .config import DATA_DIR
from .database import DEFAULT_SPACE_ID, session_for_space
from .document_extraction import extract_document_content
from .file_inspector import (
    decode_text,
    inspect_file,
    normalize_row,
    unique_columns,
)
from .models import (
    DataAsset,
    Dataset,
    DatasetRecord,
    IngestionRun,
    PipelineTemplate,
    StageRun,
)
from .record_storage import (
    business_record_key,
    count_dedicated_records,
    insert_dedicated_records,
    lock_dataset_for_write,
    persist_shared_records,
    prepare_dataset_storage,
    scope_record_key,
    upsert_dedicated_records,
)
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


def payload_fingerprint(
    data_type: str,
    payload: dict[str, Any],
    *,
    source_object: str = "",
) -> str:
    canonical = json.dumps(
        {
            "data_type": data_type,
            "payload": payload,
            "source_object": source_object,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_source_locator(
    run: IngestionRun,
    dataset: Dataset | None,
    payload: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    context = run.source_context or {}
    source_object = run.source_object or run.original_name
    if str(context.get("kind") or "") == "database":
        primary_key_fields = list(
            context.get("primary_key_fields")
            or (dataset.upsert_keys if dataset is not None else [])
            or []
        )
        primary_key = {
            field: payload[field]
            for field in primary_key_fields
            if field in payload and payload[field] not in (None, "")
        }
        return {
            "kind": "database",
            "table": source_object,
            "primary_key": primary_key,
            "row_number": index + 1,
        }
    if run.source_path:
        locator = {
            "kind": "file",
            "file": run.original_name,
            "row_number": index + 1,
            "source_uri": run.source_path,
        }
        if payload.get("__sheet_name") is not None:
            locator["sheet_name"] = payload["__sheet_name"]
        if payload.get("__sheet_index") is not None:
            locator["sheet_index"] = payload["__sheet_index"]
        return locator
    return {"kind": "record", "row_number": index + 1}


def _bounded_json(value: Any, limit: int = 12000) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "..."


def _result_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("inserted_count", "output_count", "record_count"):
            if isinstance(value.get(key), int):
                return int(value[key])
        for key in ("records", "rows", "items", "results"):
            if isinstance(value.get(key), list):
                return len(value[key])
        return 1 if value else 0
    return 1 if value is not None else 0


def _max_result_metric(value: Any, metric: str) -> int:
    if isinstance(value, dict):
        own = value.get(metric)
        own_value = own if isinstance(own, int) else 0
        return max(
            own_value,
            *(_max_result_metric(item, metric) for item in value.values()),
        )
    if isinstance(value, list):
        return max((_max_result_metric(item, metric) for item in value), default=0)
    return 0


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

    if file_type in {"pdf", "document"}:
        extracted = extract_document_content(file_type, content)
        return [], extracted.metadata, extracted.text

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


def _run_configured_pipeline(
    db,
    run: IngestionRun,
    template: PipelineTemplate,
    content: bytes,
    stages: dict[str, StageRun],
    set_active_stage,
) -> tuple[int, int, int]:
    from .mcp_server import _execute_user_pipeline

    input_dir = DATA_DIR / "pipeline-inputs" / run.id
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / Path(run.original_name).name
    input_path.write_bytes(content)
    dataset = db.get(Dataset, run.dataset_id)
    before_count = (
        count_dedicated_records(db.get_bind(), dataset.physical_table)
        if dataset is not None and dataset.physical_table
        else (
            db.scalar(
                select(func.count(DatasetRecord.id)).where(
                    DatasetRecord.dataset_id == run.dataset_id
                )
            )
            or 0
        )
    )

    def on_step_start(
        step: dict[str, Any],
        arguments: dict[str, Any],
        index: int,
    ) -> None:
        stage = stages.get(str(step.get("key") or f"step-{index}"))
        if stage is None:
            stage = next(
                (
                    item
                    for item in stages.values()
                    if item.sequence == index
                ),
                None,
            )
        set_active_stage(stage)
        if stage is not None:
            set_stage(
                db,
                stage,
                status="running",
                input_count=1,
                summary={
                    "tool_name": step.get("tool_name", ""),
                    "arguments": arguments,
                },
                logs=[f"正在调用 MCP 工具：{step.get('tool_name', '')}"],
            )

    def on_step_result(result: dict[str, Any], _index: int) -> None:
        stage = stages.get(str(result.get("key") or ""))
        if stage is not None:
            set_stage(
                db,
                stage,
                status="success",
                input_count=1,
                output_count=_result_count(result.get("result")),
                summary={
                    "tool_name": result.get("tool_name", ""),
                    "arguments": result.get("arguments", {}),
                    "result_preview": _bounded_json(result.get("result")),
                },
                logs=[f"MCP 工具执行完成：{result.get('tool_name', '')}"],
            )

    try:
        execution_results = _execute_user_pipeline(
            template.code,
            {
                "code": template.code,
                "name": template.name,
                "steps": template.steps,
            },
            inputs={
                "file_path": str(input_path),
                "filename": run.original_name,
                "file_type": run.file_type,
                "media_type": run.media_type,
                "dataset_id": run.dataset_id,
                "source_id": run.source_id or "",
                "run_id": run.id,
                "ingestion_template": "auto",
            },
            step_inputs={},
            on_step_start=on_step_start,
            on_step_result=on_step_result,
        )
    finally:
        shutil.rmtree(input_dir, ignore_errors=True)

    db.expire_all()
    dataset = db.get(Dataset, run.dataset_id)
    after_count = (
        count_dedicated_records(db.get_bind(), dataset.physical_table)
        if dataset is not None and dataset.physical_table
        else (
            db.scalar(
                select(func.count(DatasetRecord.id)).where(
                    DatasetRecord.dataset_id == run.dataset_id
                )
            )
            or 0
        )
    )
    inserted = max(
        max(
            (
                _max_result_metric(item.get("result"), "inserted_count")
                for item in execution_results
            ),
            default=0,
        ),
        after_count - before_count,
    )
    duplicates = max(
        (
            _max_result_metric(item.get("result"), "duplicate_count")
            for item in execution_results
        ),
        default=0,
    )
    updated = max(
        (
            _max_result_metric(item.get("result"), "updated_count")
            for item in execution_results
        ),
        default=0,
    )
    if dataset is not None:
        dataset.record_count = after_count
        if inserted > 0:
            dataset.last_ingested_at = utc_now()
    return inserted, updated, duplicates


def run_pipeline(
    run_id: str,
    space_id: str = DEFAULT_SPACE_ID,
) -> set[str] | None:
    db = session_for_space(space_id)
    active_stage: StageRun | None = None
    snapshot_fingerprints: set[str] | None = None
    try:
        run = db.get(IngestionRun, run_id)
        if run is None:
            return None
        run.status = "running"
        run.started_at = utc_now()
        db.commit()

        stages = {stage.stage_key: stage for stage in run.stages}
        file_type = run.file_type
        template = db.scalar(
            select(PipelineTemplate).where(PipelineTemplate.code == run.template_code)
        )

        def activate_stage(stage: StageRun | None) -> None:
            nonlocal active_stage
            active_stage = stage

        active_stage = stages.get("receive")
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="running",
                input_count=1,
                summary={"filename": run.original_name, "phase": "reading"},
            )
        content = storage.read_bytes(run.source_path)

        active_stage = stages.get("fingerprint")
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="running",
                input_count=1,
                summary={"phase": "hashing"},
            )
        run.fingerprint = sha256_bytes(content)
        db.commit()

        if run.template_code.startswith("user-"):
            if template is None or not template.enabled:
                raise ValueError("用户管线不存在或已停用")
            inserted, updated, duplicates = _run_configured_pipeline(
                db,
                run,
                template,
                content,
                stages,
                activate_stage,
            )
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
            run.fetched_count = 1
            run.inserted_count = inserted
            run.updated_count = updated
            run.duplicate_count = duplicates
            run.error_count = 0
            run.status = "success"
            run.finished_at = utc_now()
            db.commit()
            return None

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
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="running",
                input_count=1,
                summary={"phase": "validating"},
            )
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
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="running",
                input_count=1,
                summary={"phase": "extracting"},
            )
        rows, metadata, extracted_text = extract_rows(file_type, content)
        extracted_count = len(rows) if rows else (1 if extracted_text.strip() else 0)
        run.fetched_count = extracted_count
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
            extracted_table_count = int(metadata.get("table_count") or 0)
            extraction_logs = [f"识别到 {extracted_count} 条可入库单元。"]
            if file_type in {"pdf", "document"}:
                extraction_logs.append(
                    f"已提取 {extracted_table_count} 个表格并转换为 Markdown。"
                    if extracted_table_count
                    else "未检测到可识别的表格，已保留文档正文。"
                )
            if extracted_text:
                extraction_logs.append("正文和表格已写入中间产物。")
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
                logs=extraction_logs,
            )

        active_stage = stages.get("normalize")
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="running",
                input_count=extracted_count,
                summary={"phase": "normalizing"},
            )
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
        updated = 0
        duplicates = 0
        payloads: list[dict[str, Any]] = []
        fingerprint_scope = str(
            (run.source_context or {}).get("dedupe_scope") or ""
        )
        if normalized_rows:
            payloads = [
                {
                    "data_type": "row",
                    "fingerprint": payload_fingerprint(
                        "row",
                        row,
                        source_object=fingerprint_scope,
                    ),
                    "payload": row,
                }
                for row in normalized_rows
            ]
        elif normalized_text or file_type in {"image", "video"}:
            payloads = [
                {
                    "data_type": "document" if normalized_text else file_type,
                    "fingerprint": payload_fingerprint(
                        "document" if normalized_text else file_type,
                        {"sha256": run.fingerprint},
                        source_object=fingerprint_scope,
                    ),
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
        snapshot_fingerprints = {
            str(item["fingerprint"]) for item in payloads
        }
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="running",
                input_count=len(payloads),
                summary={
                    "phase": "writing",
                    "total": len(payloads),
                },
            )
        dataset = lock_dataset_for_write(db, run.dataset_id)
        upsert_keys = list(dataset.upsert_keys or []) if dataset is not None else []
        if upsert_keys:
            for item in payloads:
                record_key = business_record_key(
                    upsert_keys,
                    item["payload"],
                )
                if not record_key:
                    raise ValueError(
                        "配置的业务主键字段在数据中不存在或为空："
                        + ", ".join(upsert_keys)
                    )
                if fingerprint_scope:
                    record_key = scope_record_key(
                        record_key,
                        fingerprint_scope,
                    )
                item["record_key"] = record_key
        dedicated_table = (
            prepare_dataset_storage(
                db,
                dataset,
                incoming_count=len(payloads),
                at=run.started_at or utc_now(),
            )
            if dataset is not None
            else ""
        )
        if dedicated_table:
            now = utc_now()
            dedicated_records = [
                {
                    "id": f"{run.id[:20]}{index:012x}"[:32],
                    "run_id": run.id,
                    "fingerprint": item["fingerprint"],
                    "record_key": item.get("record_key", ""),
                    "data_type": item["data_type"],
                    "asset_path": (
                        run.source_path
                        if item["data_type"] in {"document", "image", "video"}
                        else ""
                    ),
                    "source_id": run.source_id,
                    "source_object": run.source_object or run.original_name,
                    "source_locator": record_source_locator(
                        run,
                        dataset,
                        item["payload"],
                        index,
                    ),
                    "row_index": index,
                    "payload": item["payload"],
                    "created_at": now,
                }
                for index, item in enumerate(payloads)
            ]
            if upsert_keys:
                inserted, updated, duplicates = upsert_dedicated_records(
                    db.get_bind(),
                    dedicated_table,
                    dedicated_records,
                )
            else:
                inserted = insert_dedicated_records(
                    db.get_bind(),
                    dedicated_table,
                    dedicated_records,
                )
                duplicates = max(0, len(payloads) - inserted)
        else:
            inserted, updated, duplicates = persist_shared_records(
                db,
                run.dataset_id,
                [
                    {
                        "run_id": run.id,
                        "fingerprint": item["fingerprint"],
                        "record_key": item.get("record_key", ""),
                        "data_type": item["data_type"],
                        "payload": item["payload"],
                        "asset_path": (
                            run.source_path
                            if item["data_type"] in {"document", "image", "video"}
                            else ""
                        ),
                        "source_id": run.source_id,
                        "source_object": run.source_object or run.original_name,
                        "source_locator": record_source_locator(
                            run,
                            dataset,
                            item["payload"],
                            index,
                        ),
                    }
                    for index, item in enumerate(payloads)
                ],
            )
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
        run.updated_count = updated
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
                    "updated": updated,
                    "duplicates": duplicates,
                    "target_table": (
                        db.get(Dataset, run.dataset_id).target_table
                        if db.get(Dataset, run.dataset_id)
                        else ""
                    ),
                },
                logs=[
                    f"新增 {inserted} 条记录。",
                    f"更新 {updated} 条记录。",
                    f"跳过重复记录 {duplicates} 条。",
                ],
            )

        active_stage = stages.get("verify")
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="running",
                input_count=len(payloads),
                summary={"phase": "verifying"},
            )
        dataset = db.get(Dataset, run.dataset_id)
        total_records = (
            count_dedicated_records(db.get_bind(), dedicated_table)
            if dedicated_table
            else (
                db.scalar(
                    select(func.count(DatasetRecord.id)).where(
                        DatasetRecord.dataset_id == run.dataset_id
                    )
                )
                or 0
            )
        )
        if dataset:
            dataset.record_count = total_records
            dataset.last_ingested_at = utc_now()
        if active_stage:
            set_stage(
                db,
                active_stage,
                status="success",
                input_count=len(payloads),
                output_count=total_records,
                summary={
                    "dataset_total": total_records,
                    "batch_inserted": inserted,
                    "batch_updated": updated,
                    "batch_duplicates": duplicates,
                },
                logs=["数据库数量核验通过，数据集目录已更新。"],
            )
        run.status = "success"
        run.finished_at = utc_now()
        run.error_count = 0
        db.commit()
        return snapshot_fingerprints
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
    return None
