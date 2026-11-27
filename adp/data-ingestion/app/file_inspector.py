from __future__ import annotations

import csv
import io
import mimetypes
from pathlib import Path
from typing import Any

from .document_extraction import extract_document_content


PIPELINE_BY_TYPE = {
    "csv": "tabular_standard",
    "excel": "tabular_standard",
    "pdf": "document_standard",
    "document": "document_standard",
    "image": "media_standard",
    "video": "media_standard",
}


def normalize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value.strip() if isinstance(value, str) else value
    return str(value)


def normalize_row(row: dict[Any, Any]) -> dict[str, Any]:
    return {
        str(key).strip() if str(key).strip() else f"column_{index + 1}": normalize_value(
            value
        )
        for index, (key, value) in enumerate(row.items())
    }


def detect_file_type(filename: str, content: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    guessed_type, _ = mimetypes.guess_type(filename)
    media_type = guessed_type or "application/octet-stream"
    if suffix == ".csv":
        return "csv", "text/csv"
    if suffix == ".xls":
        raise ValueError("暂不支持旧版 .xls Excel，请另存为 .xlsx 或 .xlsm 后再导入")
    if suffix in {".xlsx", ".xlsm"}:
        return "excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".pdf":
        return "pdf", "application/pdf"
    if suffix == ".doc":
        raise ValueError("暂不支持旧版 .doc Word，请另存为 .docx 后再导入")
    if suffix == ".docx":
        return "document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if media_type.startswith("image/"):
        return "image", media_type
    if media_type.startswith("video/"):
        return "video", media_type
    if content.startswith(b"%PDF"):
        return "pdf", "application/pdf"
    return "unknown", media_type


def decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def unique_columns(values: tuple[Any, ...]) -> list[str]:
    counts: dict[str, int] = {}
    columns: list[str] = []
    for index, value in enumerate(values):
        base = (
            str(value).strip()
            if value is not None and str(value).strip()
            else f"column_{index + 1}"
        )
        counts[base] = counts.get(base, 0) + 1
        columns.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return columns


def inspect_csv(content: bytes) -> dict[str, Any]:
    text = decode_text(content)
    reader = csv.DictReader(io.StringIO(text))
    columns = [str(column).strip() for column in (reader.fieldnames or [])]
    rows = []
    total = 0
    for row in reader:
        total += 1
        if len(rows) < 20:
            rows.append(normalize_row(row))
    warnings = []
    if not columns:
        warnings.append("未检测到表头，请确认文件第一行是否为字段名称。")
    return {
        "preview_type": "table",
        "columns": columns,
        "rows": rows,
        "metadata": {"row_count": total, "column_count": len(columns)},
        "warnings": warnings,
    }


def inspect_excel(content: bytes) -> dict[str, Any]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    active_sheet_name = workbook.active.title
    sheet_previews: list[dict[str, Any]] = []
    total_rows = 0
    for sheet_index, worksheet in enumerate(workbook.worksheets):
        iterator = worksheet.iter_rows(values_only=True)
        first_row = next(iterator, ())
        columns = unique_columns(first_row)
        raw_rows = []
        row_count = 0
        for values in iterator:
            if not any(value is not None and str(value).strip() for value in values):
                continue
            row_count += 1
            if len(raw_rows) < 20:
                raw_rows.append(
                    normalize_row(
                        dict(zip(columns, values, strict=False))
                    )
                )
        sheet_warnings = []
        if not columns:
            sheet_warnings.append("工作表为空或未检测到表头。")
        if worksheet.sheet_state != "visible":
            sheet_warnings.append("该工作表在 Excel 中被隐藏，但仍会参与预览和入库。")
        if row_count > 100_000:
            sheet_warnings.append("工作表记录较多，正式入库将按批次处理。")
        metadata = {
            "sheet_index": sheet_index,
            "row_count": row_count,
            "column_count": len(columns),
        }
        sheet_previews.append(
            {
                "name": worksheet.title,
                "index": sheet_index,
                "state": worksheet.sheet_state,
                "is_active": worksheet.title == active_sheet_name,
                "columns": columns,
                "rows": raw_rows,
                "metadata": metadata,
                "warnings": sheet_warnings,
            }
        )
        total_rows += row_count

    workbook.close()
    active_preview = next(
        (sheet for sheet in sheet_previews if sheet["is_active"]),
        sheet_previews[0],
    )
    metadata = {
        "sheet_name": active_preview["name"],
        "sheet_count": len(sheet_previews),
        "sheet_names": [sheet["name"] for sheet in sheet_previews],
        "active_sheet": active_preview["name"],
        "total_row_count": total_rows,
    }
    warnings = list(active_preview["warnings"])
    if len(sheet_previews) > 1:
        warnings.append("文件包含多个工作表，全部工作表都会单独预览并参与入库。")
    return {
        "preview_type": "table",
        "columns": active_preview["columns"],
        "rows": active_preview["rows"],
        "metadata": metadata,
        "warnings": warnings,
        "sheets": sheet_previews,
    }


def inspect_pdf(content: bytes) -> dict[str, Any]:
    extracted = extract_document_content("pdf", content)
    text = extracted.text
    excerpt = " ".join(text.split())[:1200]
    warnings = []
    if not excerpt:
        warnings.append("PDF 未提取到正文或表格文本，可能需要 OCR 阶段。")
    if extracted.metadata.get("table_extraction_warning"):
        warnings.append(str(extracted.metadata["table_extraction_warning"]))
    return {
        "preview_type": "document",
        "columns": [],
        "rows": [],
        "metadata": {
            **extracted.metadata,
            "excerpt": excerpt,
        },
        "warnings": warnings,
    }


def inspect_docx(content: bytes) -> dict[str, Any]:
    extracted = extract_document_content("document", content)
    text = extracted.text
    return {
        "preview_type": "document",
        "columns": [],
        "rows": [],
        "metadata": {
            **extracted.metadata,
            "excerpt": text[:1200],
        },
        "warnings": [] if text else ["文档未提取到正文或表格内容。"],
    }


def inspect_image(content: bytes) -> dict[str, Any]:
    from PIL import Image

    image = Image.open(io.BytesIO(content))
    return {
        "preview_type": "media",
        "columns": [],
        "rows": [],
        "metadata": {
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
        },
        "warnings": [],
    }


def inspect_file(filename: str, content: bytes) -> dict[str, Any]:
    file_type, media_type = detect_file_type(filename, content)
    if file_type == "csv":
        result = inspect_csv(content)
    elif file_type == "excel":
        result = inspect_excel(content)
    elif file_type == "pdf":
        result = inspect_pdf(content)
    elif file_type == "document":
        result = inspect_docx(content)
    elif file_type == "image":
        result = inspect_image(content)
    elif file_type == "video":
        result = {
            "preview_type": "media",
            "columns": [],
            "rows": [],
            "metadata": {"description": "视频文件已识别，元数据解析将在扩展阶段启用。"},
            "warnings": ["当前版本保存原始视频并记录文件元数据，尚未提取时长和关键帧。"],
        }
    else:
        result = {
            "preview_type": "unknown",
            "columns": [],
            "rows": [],
            "metadata": {},
            "warnings": ["暂未识别该文件类型，将保留原始文件后进入扩展处理流程。"],
        }
    return {
        "filename": filename,
        "file_type": file_type,
        "media_type": media_type,
        "size": len(content),
        "recommended_pipeline": PIPELINE_BY_TYPE.get(
            file_type,
            "document_standard",
        ),
        **result,
    }
