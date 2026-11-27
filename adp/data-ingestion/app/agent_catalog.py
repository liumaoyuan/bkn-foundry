from __future__ import annotations

import os
from typing import Any

from .config import AGENT_MCP_URL, PROJECT_DIR
from .data_source_service import public_source_config


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
    {
        "name": "capability_catalog",
        "display_name": "能力目录",
        "description": "读取全部 MCP 工具、Skill 和管线的完整定义。",
        "read_only": True,
        "requires_approval": False,
    },
    {
        "name": "create_mcp_tool",
        "display_name": "创建 MCP 工具",
        "description": "把用户提供的 HTTP 接口注册为新的 MCP 工具。",
        "read_only": False,
        "requires_approval": True,
    },
    {
        "name": "call_custom_mcp_tool",
        "display_name": "调用自定义 MCP",
        "description": "按工具 ID 执行用户新增的 MCP HTTP 工具。",
        "read_only": False,
        "requires_approval": True,
    },
    {
        "name": "create_skill",
        "display_name": "创建 Skill",
        "description": "创建新的用户 Skill，供 Agent 在后续任务中加载。",
        "read_only": False,
        "requires_approval": True,
    },
    {
        "name": "update_skill",
        "display_name": "编辑 Skill",
        "description": "修改用户 Skill 的 Markdown 内容、名称或说明。",
        "read_only": False,
        "requires_approval": True,
    },
    {
        "name": "create_ingestion_pipeline",
        "display_name": "创建数据管线",
        "description": "使用内置或用户 MCP 工具组合用户数据管线，并为每一步配置参数。",
        "read_only": False,
        "requires_approval": True,
    },
    {
        "name": "run_user_pipeline",
        "display_name": "执行用户管线",
        "description": "按步骤执行用户管线，支持运行输入和前置步骤结果传递。",
        "read_only": False,
        "requires_approval": True,
    },
]

TOOL_INPUT_DETAILS: dict[str, dict[str, Any]] = {
    "api_health": {"arguments": [], "returns": "服务状态、控制库状态和时间"},
    "storage_info": {"arguments": [], "returns": "数据库落点、对象存储和空间信息"},
    "list_datasets": {
        "arguments": [{"name": "query", "type": "string", "required": False}],
        "returns": "数据集 ID、名称、类型、记录数和最近入库时间",
    },
    "get_dataset_schema": {
        "arguments": [
            {"name": "dataset_id", "type": "string", "required": True},
            {"name": "sample_size", "type": "integer", "required": False},
        ],
        "returns": "抽样字段、类型、可空性和样本数",
    },
    "get_dataset_record_changes": {
        "arguments": [
            {"name": "dataset_id", "type": "string", "required": True},
            {"name": "cursor", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
        ],
        "returns": "增量记录、下一页游标和是否还有数据",
    },
    "list_pipeline_templates": {"arguments": [], "returns": "全部管线及步骤"},
    "inspect_local_file": {
        "arguments": [{"name": "file_path", "type": "string", "required": True}],
        "returns": "文件类型、预览、Sheet、警告和推荐管线",
    },
    "ingest_local_file": {
        "arguments": [
            {"name": "file_path", "type": "string", "required": True},
            {"name": "dataset_id", "type": "string", "required": True},
            {"name": "template_code", "type": "string", "required": False},
            {"name": "source_id", "type": "string", "required": False},
            {"name": "wait", "type": "boolean", "required": False},
        ],
        "returns": "入库任务、阶段、插入数和重复数",
    },
    "ingest_text": {
        "arguments": [
            {"name": "content", "type": "string", "required": True},
            {"name": "filename", "type": "string", "required": True},
            {"name": "dataset_id", "type": "string", "required": True},
            {"name": "template_code", "type": "string", "required": False},
            {"name": "wait", "type": "boolean", "required": False},
        ],
        "returns": "入库任务和最终核验结果",
    },
    "get_ingestion_run": {
        "arguments": [{"name": "run_id", "type": "string", "required": True}],
        "returns": "任务状态、阶段详情和数量统计",
    },
    "query_dataset_records": {
        "arguments": [
            {"name": "dataset_id", "type": "string", "required": True},
            {"name": "page", "type": "integer", "required": False},
            {"name": "page_size", "type": "integer", "required": False},
            {"name": "query", "type": "string", "required": False},
        ],
        "returns": "规范记录分页结果",
    },
    "list_scan_jobs": {"arguments": [], "returns": "自动扫描计划和状态"},
    "create_scan_job": {
        "arguments": [
            {"name": "name", "type": "string", "required": True},
            {"name": "source_id", "type": "string", "required": True},
            {"name": "dataset_id", "type": "string", "required": True},
            {"name": "scan_type", "type": "string", "required": True},
            {"name": "schedule_type", "type": "string", "required": False},
        ],
        "returns": "新扫描计划及下次执行时间",
    },
    "get_scan_job": {
        "arguments": [{"name": "job_id", "type": "string", "required": True}],
        "returns": "计划、状态和最近错误",
    },
    "run_scan_job_now": {
        "arguments": [{"name": "job_id", "type": "string", "required": True}],
        "returns": "扫描后的计划状态",
    },
    "capability_catalog": {"arguments": [], "returns": "自定义 MCP、Skill 和全部管线"},
    "create_mcp_tool": {
        "arguments": [
            {"name": "name", "type": "string", "required": True},
            {"name": "display_name", "type": "string", "required": True},
            {"name": "description", "type": "string", "required": True},
            {"name": "url", "type": "string", "required": True},
            {"name": "method", "type": "string", "required": False},
            {"name": "headers", "type": "object", "required": False},
            {"name": "read_only", "type": "boolean", "required": False},
            {"name": "requires_approval", "type": "boolean", "required": False},
        ],
        "returns": "新 MCP 工具定义",
    },
    "call_custom_mcp_tool": {
        "arguments": [
            {"name": "tool_id", "type": "string", "required": True},
            {"name": "arguments", "type": "object", "required": True},
        ],
        "returns": "被调用工具的状态码和响应内容",
    },
    "create_skill": {
        "arguments": [
            {"name": "name", "type": "string", "required": True},
            {"name": "display_name", "type": "string", "required": True},
            {"name": "description", "type": "string", "required": True},
            {"name": "content", "type": "string", "required": True},
        ],
        "returns": "新 Skill 定义",
    },
    "update_skill": {
        "arguments": [
            {"name": "skill_id", "type": "string", "required": True},
            {"name": "content", "type": "string", "required": True},
        ],
        "returns": "更新后的 Skill",
    },
    "create_ingestion_pipeline": {
        "arguments": [
            {"name": "code", "type": "string", "required": True},
            {"name": "name", "type": "string", "required": True},
            {"name": "description", "type": "string", "required": True},
            {"name": "supported_types", "type": "array", "required": True},
            {"name": "steps", "type": "array", "required": True},
        ],
        "returns": "新用户管线定义",
    },
    "run_user_pipeline": {
        "arguments": [
            {"name": "pipeline_code", "type": "string", "required": True},
            {"name": "inputs", "type": "object", "required": False},
            {"name": "step_inputs", "type": "object", "required": False},
        ],
        "returns": "每个管线步骤的执行结果",
    },
}


def get_agent_manifest(db=None) -> dict[str, Any]:
    skill_content = SKILL_PATH.read_text(encoding="utf-8") if SKILL_PATH.exists() else ""
    custom_tools = []
    custom_skills = []
    if db is not None:
        from .models import McpToolDefinition, SkillDefinition

        custom_tools = [
            {
                "id": tool.id,
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "read_only": tool.read_only,
                "requires_approval": tool.requires_approval,
                "tool_type": tool.tool_type,
                "config": public_source_config(tool.config),
                "enabled": tool.enabled,
                "built_in": False,
                "locked": False,
            }
            for tool in db.query(McpToolDefinition)
            .order_by(McpToolDefinition.created_at.asc())
            .all()
        ]
        custom_skills = [
            {
                "id": skill.id,
                "name": skill.name,
                "display_name": skill.display_name,
                "description": skill.description,
                "enabled": skill.enabled,
                "built_in": False,
                "locked": False,
            }
            for skill in db.query(SkillDefinition)
            .order_by(SkillDefinition.created_at.asc())
            .all()
        ]
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
            "built_in": True,
            "locked": True,
            "custom": custom_skills,
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
                "built_in": True,
                "locked": True,
                **TOOL_INPUT_DETAILS.get(tool["name"], {}),
                "qualifiedName": f"mcp__marine_ingestion__{tool['name']}",
            }
            for tool in MCP_TOOLS
        ]
        + custom_tools,
    }


def get_skill_content(db=None) -> dict[str, Any]:
    if not SKILL_PATH.exists():
        return {
            "name": "multisource-ingestion",
            "path": str(SKILL_PATH),
            "content": "",
            "loaded": False,
        }
    result = {
        "name": "multisource-ingestion",
        "path": str(SKILL_PATH),
        "content": SKILL_PATH.read_text(encoding="utf-8"),
        "loaded": True,
        "built_in": True,
        "locked": True,
    }
    if db is not None:
        from .models import SkillDefinition

        result["custom"] = [
            {
                "id": skill.id,
                "name": skill.name,
                "display_name": skill.display_name,
                "description": skill.description,
                "content": skill.content,
                "enabled": skill.enabled,
                "built_in": False,
                "locked": False,
            }
            for skill in db.query(SkillDefinition)
            .order_by(SkillDefinition.created_at.asc())
            .all()
        ]
    return result
