from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DataSource, Dataset, PipelineTemplate


def pipeline_step(
    key: str,
    name: str,
    kind: str,
    description: str,
    input_name: str,
    output_name: str,
    implementation: str,
    parameters: dict,
    artifact: str = "",
    retryable: bool = True,
) -> dict:
    return {
        "key": key,
        "name": name,
        "kind": kind,
        "description": description,
        "input": input_name,
        "output": output_name,
        "implementation": implementation,
        "parameters": parameters,
        "artifact": artifact,
        "retryable": retryable,
    }


TEMPLATES = [
    {
        "code": "tabular_standard",
        "name": "结构化表格入库",
        "description": "适用于 CSV、Excel。逐个解析工作表，规范字段与空行后写入规范记录。",
        "supported_types": ["csv", "excel"],
        "steps": [
            pipeline_step(
                "receive",
                "接收文件",
                "source",
                "把上传文件写入本地原始目录或 S3/MinIO，保留原始文件名和媒体类型。",
                "上传文件",
                "原始对象 URI",
                "storage.save_bytes",
                {"storage_backend": "local | s3", "namespace": "raw/<run_id>"},
                "raw/<run_id>/<filename>",
            ),
            pipeline_step(
                "fingerprint",
                "生成指纹",
                "integrity",
                "计算文件 SHA-256，作为数据血缘和重复判断依据。",
                "原始字节",
                "SHA-256",
                "hashlib.sha256",
                {"algorithm": "sha256"},
            ),
            pipeline_step(
                "validate",
                "格式校验",
                "validation",
                "确认文件类型与模板匹配，检测表头、工作表、行数和字段预览。",
                "原始文件",
                "校验结果和警告",
                "file_inspector.inspect_file",
                {"preview_rows": 20, "strict_type_match": True},
            ),
            pipeline_step(
                "extract",
                "解析表格",
                "extract",
                "CSV 使用字典读取；Excel 遍历全部工作表并为每行附加 Sheet 来源。",
                "有效表格文件",
                "规范前的数据行",
                "csv.DictReader | openpyxl",
                {"all_excel_sheets": True, "empty_row_filter": True},
            ),
            pipeline_step(
                "normalize",
                "清洗规范",
                "transform",
                "处理重复表头、空字段、首尾空白、连续空格和完全空白的数据行。",
                "原始数据行",
                "待入库规范行",
                "pipeline.run_pipeline",
                {"trim": True, "collapse_whitespace": True, "drop_empty_rows": True},
            ),
            pipeline_step(
                "persist",
                "写入数据库",
                "load",
                "按 dataset_id 写入 dataset_records；使用文件指纹与行序号实现幂等 Upsert。",
                "规范行",
                "规范化记录和资产记录",
                "SQLAlchemy",
                {"target": "dataset_records", "idempotent": True},
            ),
            pipeline_step(
                "verify",
                "结果核验",
                "verification",
                "核对新增、重复、错误数量，并更新数据集总记录数和最近入库时间。",
                "本次批次统计",
                "最终任务结果",
                "SQLAlchemy count",
                {"check_dataset_total": True},
            ),
        ],
    },
    {
        "code": "document_standard",
        "name": "文档内容入库",
        "description": "适用于 PDF、DOCX。提取正文与元数据，保留原始文件并建立入库记录。",
        "supported_types": ["pdf", "document"],
        "steps": [
            pipeline_step(
                "receive",
                "接收文件",
                "source",
                "保存原始文档和存储 URI。",
                "上传文档",
                "原始对象 URI",
                "storage.save_bytes",
                {"storage_backend": "local | s3", "namespace": "raw/<run_id>"},
                "raw/<run_id>/<filename>",
            ),
            pipeline_step(
                "fingerprint",
                "生成指纹",
                "integrity",
                "计算文档内容 SHA-256。",
                "原始字节",
                "SHA-256",
                "hashlib.sha256",
                {"algorithm": "sha256"},
            ),
            pipeline_step(
                "validate",
                "格式校验",
                "validation",
                "验证 PDF/DOCX 结构并生成页数、段落数、字符数和文本警告。",
                "原始文档",
                "文档元数据和警告",
                "pypdf | python-docx",
                {"extract_excerpt": 1200},
            ),
            pipeline_step(
                "extract",
                "提取正文",
                "extract",
                "逐页提取 PDF 文本或读取 DOCX 非空段落。",
                "有效文档",
                "原始正文",
                "pypdf.PdfReader | python-docx.Document",
                {"ocr_fallback": False},
                "artifacts/<run_id>/extracted.txt",
            ),
            pipeline_step(
                "normalize",
                "文本清洗",
                "transform",
                "按段落清理多余空白并保留段落边界。",
                "原始正文",
                "规范正文",
                "pipeline.run_pipeline",
                {"drop_empty_paragraphs": True, "collapse_whitespace": True},
            ),
            pipeline_step(
                "persist",
                "归档入库",
                "load",
                "原始文档作为资产，正文预览与文档元数据写入规范记录。",
                "规范正文和资产",
                "文档记录与资产记录",
                "SQLAlchemy",
                {"record_type": "document", "keep_text_preview_chars": 4000},
            ),
            pipeline_step(
                "verify",
                "结果核验",
                "verification",
                "核对文档记录、资产数量和数据集总量。",
                "本批次统计",
                "最终任务结果",
                "SQLAlchemy count",
                {"check_asset": True, "check_dataset_total": True},
            ),
        ],
    },
    {
        "code": "media_standard",
        "name": "影像资产入库",
        "description": "适用于图片、视频。读取媒体元数据、建立资产记录并保留原文件。",
        "supported_types": ["image", "video"],
        "steps": [
            pipeline_step(
                "receive",
                "接收文件",
                "source",
                "把图片或视频写入原始对象存储。",
                "上传媒体文件",
                "原始对象 URI",
                "storage.save_bytes",
                {"storage_backend": "local | s3", "namespace": "raw/<run_id>"},
                "raw/<run_id>/<filename>",
            ),
            pipeline_step(
                "fingerprint",
                "生成指纹",
                "integrity",
                "计算媒体文件 SHA-256。",
                "原始字节",
                "SHA-256",
                "hashlib.sha256",
                {"algorithm": "sha256"},
            ),
            pipeline_step(
                "validate",
                "格式校验",
                "validation",
                "依据扩展名和 MIME 识别图片或视频类型。",
                "原始媒体",
                "媒体类型",
                "mimetypes",
                {"media_family_check": True},
            ),
            pipeline_step(
                "extract",
                "读取元数据",
                "extract",
                "图片读取格式、尺寸和色彩模式；视频当前记录基础文件信息。",
                "有效媒体文件",
                "媒体元数据",
                "Pillow",
                {"image_metadata": True, "video_probe": False},
            ),
            pipeline_step(
                "normalize",
                "规范资产",
                "transform",
                "整理文件名、媒体类型、大小和元数据字段。",
                "媒体元数据",
                "规范资产信息",
                "pipeline.run_pipeline",
                {"bounded_metadata": True},
            ),
            pipeline_step(
                "persist",
                "资产归档",
                "load",
                "写入媒体资产和规范记录，原始二进制保留在对象存储。",
                "规范资产信息",
                "媒体资产记录",
                "SQLAlchemy",
                {"record_type": "image | video", "store_binary_in_db": False},
            ),
            pipeline_step(
                "verify",
                "结果核验",
                "verification",
                "核对资产 URI、大小、媒体类型和数据集总量。",
                "本批次统计",
                "最终任务结果",
                "SQLAlchemy count",
                {"check_asset": True, "check_dataset_total": True},
            ),
        ],
    },
]


DATASETS = [
    {
        "name": "台湾近海观测数据",
        "slug": "taiwan-marine-observations",
        "description": "台湾周边海洋站点、海况观测和结构化业务数据。",
        "data_type": "tabular",
        "target_table": "taiwan_marine_observations",
    },
    {
        "name": "海洋公开文档",
        "slug": "marine-public-documents",
        "description": "海洋政策、航路资料、研究报告等 PDF 与 DOCX 文档。",
        "data_type": "document",
        "target_table": "marine_documents",
    },
    {
        "name": "多源影像资产",
        "slug": "multi-source-media",
        "description": "遥感、现场照片和视频资料的统一资产目录。",
        "data_type": "media",
        "target_table": "media_assets",
    },
]


def seed_database(db: Session, *, include_examples: bool = True) -> None:
    existing_templates = {
        item.code: item
        for item in db.scalars(select(PipelineTemplate)).all()
    }
    for item in TEMPLATES:
        existing = existing_templates.get(item["code"])
        if existing:
            existing.name = item["name"]
            existing.description = item["description"]
            existing.supported_types = item["supported_types"]
            existing.steps = item["steps"]
            existing.enabled = True
        else:
            db.add(PipelineTemplate(**item))

    if include_examples and not db.scalar(select(Dataset).limit(1)):
        for item in DATASETS:
            db.add(Dataset(**item))

    if include_examples and not db.scalar(select(DataSource).limit(1)):
        db.add(
            DataSource(
                name="本地上传",
                source_type="upload",
                status="ready",
                config={"description": "通过页面直接上传文件"},
            )
        )
    db.commit()
