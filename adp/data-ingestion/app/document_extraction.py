# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

from dataclasses import dataclass
import io
import logging
from typing import Any, Iterable


logger = logging.getLogger(__name__)

MAX_TABLE_PREVIEWS = 10
MAX_TABLE_PREVIEW_ROWS = 6
PDF_LINE_TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
}
PDF_TEXT_TABLE_SETTINGS = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
}


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    metadata: dict[str, Any]


def _normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    lines = [
        " ".join(line.split())
        for line in str(value).replace("\xa0", " ").splitlines()
    ]
    return "<br>".join(line for line in lines if line)


def _normalize_table_rows(raw_rows: Iterable[Iterable[Any]]) -> list[list[str]]:
    rows = []
    for raw_row in raw_rows:
        row = [_normalize_cell(value) for value in raw_row]
        if any(row):
            rows.append(row)
    if len(rows) < 2:
        return []
    column_count = max((len(row) for row in rows), default=0)
    if column_count < 2:
        return []
    return [row + [""] * (column_count - len(row)) for row in rows]


def table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    column_count = max((len(row) for row in rows), default=0)
    if column_count < 2:
        return ""
    header = rows[0] + [""] * (column_count - len(rows[0]))
    header = [
        (value or f"列 {index + 1}").replace("|", r"\|")
        for index, value in enumerate(header)
    ]
    body = [
        [value.replace("|", r"\|") for value in row]
        + [""] * (column_count - len(row))
        for row in rows[1:]
    ]
    lines = [
        f"| {' | '.join(header)} |",
        f"| {' | '.join('---' for _ in range(column_count))} |",
        *[f"| {' | '.join(row)} |" for row in body],
    ]
    return "\n".join(lines)


def _table_metadata(tables: list[dict[str, Any]]) -> dict[str, Any]:
    previews = [
        {
            **table,
            "rows": table["rows"][:MAX_TABLE_PREVIEW_ROWS],
        }
        for table in tables[:MAX_TABLE_PREVIEWS]
    ]
    return {
        "table_count": len(tables),
        "table_row_count": sum(table["row_count"] for table in tables),
        "tables": previews,
        "table_preview_truncated": (
            len(tables) > MAX_TABLE_PREVIEWS
            or any(
                table["row_count"] > MAX_TABLE_PREVIEW_ROWS
                for table in tables[:MAX_TABLE_PREVIEWS]
            )
        ),
    }


def _table_block(table: dict[str, Any]) -> str:
    location = (
        f"第 {table['page']} 页"
        if table.get("page") is not None
        else "正文内嵌"
    )
    return "\n\n".join(
        [
            f"## 表格 {table['index']}（{location}）",
            table_to_markdown(table["rows"]),
        ]
    )


def _extract_docx(content: bytes) -> ExtractedDocument:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(content))
    tables: list[dict[str, Any]] = []
    blocks: list[str] = []
    paragraph_count = 0

    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            value = " ".join(paragraph.text.split())
            if not value:
                continue
            paragraph_count += 1
            blocks.append(value)
            continue
        if not child.tag.endswith("}tbl"):
            continue

        table = Table(child, document)
        seen_cells: set[Any] = set()
        raw_rows: list[list[str]] = []
        for row in table.rows:
            values = []
            for cell in row.cells:
                marker = cell._tc
                if marker in seen_cells:
                    values.append("")
                    continue
                seen_cells.add(marker)
                values.append(cell.text)
            raw_rows.append(values)
        rows = _normalize_table_rows(raw_rows)
        if not rows:
            continue
        table = {
            "source": "docx",
            "index": len(tables) + 1,
            "row_count": len(rows),
            "column_count": len(rows[0]),
            "rows": rows,
        }
        tables.append(table)
        blocks.append(_table_block(table))

    text = "\n\n".join(blocks).strip()
    return ExtractedDocument(
        text=text,
        metadata={
            "paragraph_count": paragraph_count,
            "character_count": len(text),
            **_table_metadata(tables),
        },
    )


def _pdf_tables(content: bytes) -> list[dict[str, Any]]:
    import pdfplumber

    tables: list[dict[str, Any]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            raw_tables = page.extract_tables(PDF_LINE_TABLE_SETTINGS)
            if not raw_tables:
                raw_tables = page.extract_tables(PDF_TEXT_TABLE_SETTINGS)
            for raw_table in raw_tables:
                rows = _normalize_table_rows(raw_table)
                if not rows:
                    continue
                tables.append(
                    {
                        "source": "pdf",
                        "index": len(tables) + 1,
                        "page": page_number,
                        "row_count": len(rows),
                        "column_count": len(rows[0]),
                        "rows": rows,
                    }
                )
    return tables


def _extract_pdf(content: bytes) -> ExtractedDocument:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    table_warning = ""
    try:
        tables = _pdf_tables(content)
    except Exception:
        logger.exception("PDF table extraction failed; continuing with text", exc_info=True)
        tables = []
        table_warning = (
            "PDF 表格结构识别失败，已保留文本层内容；表格可能需要转为 DOCX 后重试。"
        )
    tables_by_page: dict[int, list[dict[str, Any]]] = {}
    for table in tables:
        tables_by_page.setdefault(int(table["page"]), []).append(table)
    page_blocks: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        page_text = (page.extract_text() or "").strip()
        page_tables = tables_by_page.get(page_number, [])
        if page_text:
            page_blocks.append(f"## 第 {page_number} 页\n\n{page_text}")
        page_blocks.extend(_table_block(table) for table in page_tables)
    text = "\n\n".join(page_blocks).strip()
    metadata = {
        "page_count": len(reader.pages),
        "character_count": len(text),
        **_table_metadata(tables),
    }
    if table_warning:
        metadata["table_extraction_warning"] = table_warning
    return ExtractedDocument(text=text, metadata=metadata)


def extract_document_content(file_type: str, content: bytes) -> ExtractedDocument:
    if file_type == "pdf":
        return _extract_pdf(content)
    if file_type == "document":
        return _extract_docx(content)
    raise ValueError(f"不支持的文档类型：{file_type}")
