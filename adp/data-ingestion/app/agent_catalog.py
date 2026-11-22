from __future__ import annotations

import os
from typing import Any

from .config import AGENT_MCP_URL, PROJECT_DIR


SKILL_DIR = PROJECT_DIR / ".agents" / "skills" / "multisource-ingestion"
SKILL_PATH = SKILL_DIR / "SKILL.md"
DSH_CONFIG_PATH = PROJECT_DIR / "docs" / "dsh-mcp.example.yml"

MCP_TOOLS = [
    {
        "name": "api_health",
        "display_name": "服务健康检查",
        "description": "检查入库 API 是否可访问。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "storage_info",
        "display_name": "存储落点",
        "description": "查看当前 MariaDB 与本地文件或 OpenBKN MinIO 落点。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "list_datasets",
        "display_name": "数据集目录",
        "description": "列出可写入的数据集，避免 Agent 猜测 dataset_id。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "get_dataset_schema",
        "display_name": "数据集结构",
        "description": "读取规范记录字段名、数据类型和可空性，供下游抽取使用。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "get_dataset_record_changes",
        "display_name": "增量读取规范数据",
        "description": "按稳定游标分批读取数据集新增记录，供增量处理使用。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "list_pipeline_templates",
        "display_name": "入库管线目录",
        "description": "读取管线模板、支持文件和全部执行阶段。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "inspect_local_file",
        "display_name": "文件预览",
        "description": "识别文件、类型、样例、多 Sheet 和推荐管线，不写入数据。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "ingest_local_file",
        "display_name": "文件入库",
        "description": "将允许目录内的文件通过完整管线写入规范数据库。",
        "read_only": False,
        "requires_approval": True,
    },
    {
        "name": "ingest_text",
        "display_name": "文本内容入库",
        "description": "写入 Agent 生成的小型 CSV、JSON 或 Markdown。",
        "read_only": False,
        "requires_approval": True,
    },
    {
        "name": "get_ingestion_run",
        "display_name": "任务核验",
        "description": "读取任务状态、数量和七个执行阶段。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "query_dataset_records",
        "display_name": "规范数据查询",
        "description": "分页查询入库记录，用于验证最终结果。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "list_scan_jobs",
        "display_name": "自动扫描任务",
        "description": "查看用户配置的自动扫描、执行计划和最近状态。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "create_scan_job",
        "display_name": "创建自动扫描",
        "description": "按间隔、每日、每周或 Cron 创建自动扫描任务。",
        "read_only": False,
        "requires_approval": True,
    },
    {
        "name": "get_scan_job",
        "display_name": "扫描任务详情",
        "description": "读取扫描计划、下次执行时间和最近错误。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "run_scan_job_now",
        "display_name": "立即执行扫描",
        "description": "跳过等待立即执行一个扫描任务。",
        "read_only": False,
        "requires_approval": True,
    },
]


def get_agent_manifest() -> dict[str, Any]:
    skill_content = SKILL_PATH.read_text(encoding="utf-8") if SKILL_PATH.exists() else ""
    return {
        "id": "marine-data-ingestion-agent",
        "name": "多源数据入库 Agent",
        "description": "在用户授权范围内检查数据、选择管线、调用 MCP 工具并核验入库结果。",
        "preset": {
            "mode": "harness",
            "maxToolSteps": 8,
            "toolResultMode": "bounded",
            "permissionProfile": "workspace-write",
            "writePolicy": "requires-explicit-user-intent",
        },
        "mcp": {
            "serverName": "marine_ingestion",
            "protocol": "MCP",
            "transports": ["stdio", "streamable-http"],
            "endpoint": AGENT_MCP_URL,
            "configured": True,
        },
        "skill": {
            "name": "multisource-ingestion",
            "displayName": "多源数据入库",
            "path": str(SKILL_PATH),
            "loaded": bool(skill_content),
        },
        "dsh": {
            "configPath": str(DSH_CONFIG_PATH),
            "clientPackage": "@deepseek-ai/dsh-mcp-client",
            "toolPrefix": "mcp__marine_ingestion__",
        },
        "permissions": {
            "allowedFileRoots": [
                root
                for root in os.getenv(
                    "MCP_ALLOWED_ROOTS",
                    str(PROJECT_DIR),
                ).split(os.pathsep)
                if root.strip()
            ],
            "automaticSchemaCreation": False,
            "automaticDatasetCreation": False,
            "writeToolsRequireApproval": True,
        },
        "workflow": [
            "连接 MCP 并发现工具",
            "检查存储落点",
            "获取数据集与管线",
            "读取数据契约和增量记录",
            "预览文件",
            "执行入库",
            "核验阶段与最终记录",
            "按需创建自动扫描计划",
        ],
        "tools": [
            {
                **tool,
                "qualifiedName": f"mcp__marine_ingestion__{tool['name']}",
            }
            for tool in MCP_TOOLS
        ],
    }


def get_skill_content() -> dict[str, Any]:
    if not SKILL_PATH.exists():
        return {
            "name": "multisource-ingestion",
            "path": str(SKILL_PATH),
            "content": "",
            "loaded": False,
        }
    return {
        "name": "multisource-ingestion",
        "path": str(SKILL_PATH),
        "content": SKILL_PATH.read_text(encoding="utf-8"),
        "loaded": True,
    }
