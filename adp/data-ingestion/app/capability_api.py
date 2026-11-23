# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .agent_catalog import MCP_TOOLS
from .database import current_space_id, get_db, require_initialized_control_database
from .data_source_service import (
    prepare_source_config,
    prepare_source_config_update,
    public_source_config,
    resolved_source_config,
)
from .models import (
    McpToolDefinition,
    PipelineExecution,
    PipelineTemplate,
    SkillDefinition,
    utc_now,
)
from .schemas import (
    McpToolCreate,
    McpToolExecuteRequest,
    McpToolUpdate,
    PipelineRunRequest,
    PipelineExecutionCreate,
    PipelineExecutionUpdate,
    PipelineTemplateCreate,
    PipelineTemplateUpdate,
    SkillCreate,
    SkillUpdate,
)
from .seed import TEMPLATES


router = APIRouter(
    prefix="/api/capabilities",
    tags=["capabilities"],
    dependencies=[Depends(require_initialized_control_database)],
)

BUILTIN_PIPELINE_CODES = {item["code"] for item in TEMPLATES}
BUILTIN_MCP_TOOL_NAMES = {item["name"] for item in MCP_TOOLS}
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _custom_tool_dict(tool: McpToolDefinition) -> dict[str, Any]:
    return {
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
        "created_at": tool.created_at,
        "updated_at": tool.updated_at,
    }


def _skill_dict(skill: SkillDefinition) -> dict[str, Any]:
    return {
        "id": skill.id,
        "name": skill.name,
        "display_name": skill.display_name,
        "description": skill.description,
        "content": skill.content,
        "enabled": skill.enabled,
        "built_in": False,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
    }


def _execution_dict(execution: PipelineExecution) -> dict[str, Any]:
    return {
        "id": execution.id,
        "pipeline_code": execution.pipeline_code,
        "pipeline_name": execution.pipeline_name,
        "space_id": execution.space_id,
        "status": execution.status,
        "inputs": execution.inputs,
        "step_inputs": execution.step_inputs,
        "results": execution.results,
        "error_message": execution.error_message,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "created_at": execution.created_at,
    }


def _validate_http_config(config: dict[str, Any]) -> dict[str, Any]:
    url = str(config.get("url") or "").strip()
    method = str(config.get("method") or "POST").strip().upper()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="MCP HTTP 工具必须配置有效的 http/https URL")
    if method not in HTTP_METHODS:
        raise HTTPException(status_code=400, detail="不支持的 HTTP 方法")
    headers_value = config.get("headers")
    headers: dict[str, str] = {}
    if headers_value is not None or not config.get("headers_encrypted"):
        headers = headers_value or {}
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in headers.items()
        ):
            raise HTTPException(status_code=400, detail="HTTP headers 必须是字符串键值对")
    try:
        timeout = float(config.get("timeout_seconds") or 30)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="timeout_seconds 必须是数字") from error
    if timeout < 1 or timeout > 300:
        raise HTTPException(status_code=400, detail="timeout_seconds 必须在 1 到 300 之间")
    validated = {
        **config,
        "url": url,
        "method": method,
        "timeout_seconds": timeout,
    }
    if headers_value is not None or not config.get("headers_encrypted"):
        validated["headers"] = headers
    return validated


def _resolved_http_config(config: dict[str, Any]) -> dict[str, Any]:
    resolved = resolved_source_config(SimpleNamespace(config=config))
    return _validate_http_config(resolved)


def _validate_pipeline_steps(
    db: Session,
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    custom_names = set(
        db.scalars(
            select(McpToolDefinition.name).where(McpToolDefinition.enabled.is_(True))
        ).all()
    )
    available = BUILTIN_MCP_TOOL_NAMES | custom_names
    step_keys: set[str] = set()
    normalized_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise HTTPException(status_code=400, detail=f"第 {index} 个管线步骤格式无效")
        step_key = str(step.get("key") or f"step-{index}").strip()
        if step_key in step_keys:
            raise HTTPException(status_code=400, detail=f"管线步骤 key 重复：{step_key}")
        step_keys.add(step_key)
        tool_name = str(step.get("tool_name") or "").strip()
        if tool_name not in available:
            raise HTTPException(
                status_code=400,
                detail=f"第 {index} 个管线步骤引用了不存在的 MCP 工具：{tool_name or '<empty>'}",
            )
        parameters = step.get("parameters", {})
        if not isinstance(parameters, dict):
            raise HTTPException(status_code=400, detail=f"管线步骤 {index} 的 parameters 必须是对象")
        custom_tool = db.scalar(
            select(McpToolDefinition).where(McpToolDefinition.name == tool_name)
        )
        builtin_tool = next(
            (item for item in MCP_TOOLS if item["name"] == tool_name),
            None,
        )
        display_name = str(step.get("name") or "").strip()
        if not display_name:
            display_name = str(
                (builtin_tool or {}).get("display_name")
                or (custom_tool.display_name if custom_tool else "")
                or tool_name
            )
        normalized_steps.append(
            {
                **step,
                "key": step_key,
                "name": display_name,
                "kind": str(step.get("kind") or "mcp_tool"),
                "input": str(step.get("input") or (f"step-{index}" if index > 1 else "run input")),
                "output": str(step.get("output") or "tool result"),
                "parameters": parameters,
                "retryable": bool(step.get("retryable", True)),
            }
        )
    return normalized_steps


@router.get("/mcp-tools")
def list_custom_mcp_tools(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    tools = db.scalars(
        select(McpToolDefinition).order_by(McpToolDefinition.created_at.asc())
    ).all()
    return [_custom_tool_dict(tool) for tool in tools]


@router.post("/mcp-tools", status_code=201)
def create_mcp_tool(
    payload: McpToolCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if payload.name in BUILTIN_MCP_TOOL_NAMES:
        raise HTTPException(status_code=409, detail="不能覆盖内置 MCP 工具")
    if db.scalar(select(McpToolDefinition.id).where(McpToolDefinition.name == payload.name)):
        raise HTTPException(status_code=409, detail="MCP 工具名称已存在")
    tool = McpToolDefinition(
        name=payload.name,
        display_name=payload.display_name.strip(),
        description=payload.description.strip(),
        read_only=payload.read_only,
        requires_approval=payload.requires_approval,
        tool_type="http",
        config=prepare_source_config(_validate_http_config(payload.config)),
        enabled=payload.enabled,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return _custom_tool_dict(tool)


@router.put("/mcp-tools/{tool_id}")
def update_mcp_tool(
    tool_id: str,
    payload: McpToolUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tool = db.get(McpToolDefinition, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="MCP 工具不存在")
    values = payload.model_dump(exclude_unset=True)
    if "config" in values and values["config"] is not None:
        merged = prepare_source_config_update(tool.config or {}, values["config"])
        _resolved_http_config(merged)
        values["config"] = merged
    for key, value in values.items():
        setattr(tool, key, value)
    db.commit()
    db.refresh(tool)
    return _custom_tool_dict(tool)


@router.delete("/mcp-tools/{tool_id}", status_code=204)
def delete_mcp_tool(
    tool_id: str,
    db: Session = Depends(get_db),
) -> Response:
    tool = db.get(McpToolDefinition, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="MCP 工具不存在")
    db.delete(tool)
    db.commit()
    return Response(status_code=204)


@router.post("/mcp-tools/{tool_id}/execute")
def execute_mcp_tool(
    tool_id: str,
    payload: McpToolExecuteRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tool = db.get(McpToolDefinition, tool_id)
    if tool is None or not tool.enabled:
        raise HTTPException(status_code=404, detail="MCP 工具不存在或已停用")
    config = _resolved_http_config(tool.config)
    method = str(config["method"])
    try:
        with httpx.Client(
            timeout=float(config["timeout_seconds"]),
            follow_redirects=True,
        ) as client:
            response = client.request(
                method,
                str(config["url"]),
                headers=dict(config.get("headers") or {}),
                **(
                    {"params": payload.arguments}
                    if method in {"GET", "DELETE"}
                    else {"json": payload.arguments}
                ),
            )
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"MCP 工具请求失败：{type(error).__name__}",
        ) from error
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:20000]
    if response.is_error:
        raise HTTPException(
            status_code=502,
            detail=f"MCP 工具返回 HTTP {response.status_code}",
        )
    return {
        "tool_id": tool.id,
        "tool_name": tool.name,
        "status_code": response.status_code,
        "result": body,
    }


@router.get("/skills")
def list_custom_skills(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    skills = db.scalars(
        select(SkillDefinition).order_by(SkillDefinition.created_at.asc())
    ).all()
    return [_skill_dict(skill) for skill in skills]


@router.post("/skills", status_code=201)
def create_skill(
    payload: SkillCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if db.scalar(select(SkillDefinition.id).where(SkillDefinition.name == payload.name)):
        raise HTTPException(status_code=409, detail="Skill 名称已存在")
    skill = SkillDefinition(
        name=payload.name,
        display_name=payload.display_name.strip(),
        description=payload.description.strip(),
        content=payload.content,
        enabled=payload.enabled,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return _skill_dict(skill)


@router.put("/skills/{skill_id}")
def update_skill(
    skill_id: str,
    payload: SkillUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    skill = db.get(SkillDefinition, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(skill, key, value)
    db.commit()
    db.refresh(skill)
    return _skill_dict(skill)


@router.delete("/skills/{skill_id}", status_code=204)
def delete_skill(
    skill_id: str,
    db: Session = Depends(get_db),
) -> Response:
    skill = db.get(SkillDefinition, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    db.delete(skill)
    db.commit()
    return Response(status_code=204)


@router.post("/pipeline-executions", status_code=201)
def create_pipeline_execution(
    payload: PipelineExecutionCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    execution = PipelineExecution(
        pipeline_code=payload.pipeline_code,
        pipeline_name=payload.pipeline_name,
        space_id=current_space_id(request),
        status="running",
        inputs=payload.inputs,
        step_inputs=payload.step_inputs,
        started_at=utc_now(),
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return _execution_dict(execution)


@router.patch("/pipeline-executions/{execution_id}")
def update_pipeline_execution(
    execution_id: str,
    payload: PipelineExecutionUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    execution = db.get(PipelineExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="管线执行记录不存在")
    values = payload.model_dump(exclude_unset=True)
    if values.get("status") is not None:
        execution.status = str(values["status"])
        if execution.status in {"success", "failed"}:
            execution.finished_at = utc_now()
    if values.get("results") is not None:
        execution.results = values["results"]
    if values.get("error_message") is not None:
        execution.error_message = str(values["error_message"])
    db.commit()
    db.refresh(execution)
    return _execution_dict(execution)


@router.get("/pipeline-executions")
def list_pipeline_executions(
    limit: int = 200,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    executions = db.scalars(
        select(PipelineExecution)
        .order_by(PipelineExecution.created_at.desc())
        .limit(max(1, min(limit, 500)))
    ).all()
    return [_execution_dict(item) for item in executions]


@router.get("/pipeline-executions/page")
def page_pipeline_executions(
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    total = db.scalar(select(func.count(PipelineExecution.id))) or 0
    executions = db.scalars(
        select(PipelineExecution)
        .order_by(PipelineExecution.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_execution_dict(item) for item in executions],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.delete("/pipeline-executions/{execution_id}", status_code=204)
def delete_pipeline_execution(
    execution_id: str,
    db: Session = Depends(get_db),
) -> Response:
    execution = db.get(PipelineExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="管线执行记录不存在")
    if execution.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="运行中的管线执行不能删除")
    db.delete(execution)
    db.commit()
    return Response(status_code=204)


@router.post("/pipelines", status_code=201)
def create_pipeline(
    payload: PipelineTemplateCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if payload.code in BUILTIN_PIPELINE_CODES:
        raise HTTPException(status_code=409, detail="不能覆盖内置管线")
    if db.scalar(select(PipelineTemplate.id).where(PipelineTemplate.code == payload.code)):
        raise HTTPException(status_code=409, detail="管线代码已存在")
    steps = _validate_pipeline_steps(db, payload.steps)
    pipeline = PipelineTemplate(
        code=payload.code,
        name=payload.name.strip(),
        description=payload.description.strip(),
        supported_types=[item.strip() for item in payload.supported_types if item.strip()],
        steps=steps,
        enabled=payload.enabled,
    )
    db.add(pipeline)
    db.commit()
    db.refresh(pipeline)
    return {
        "id": pipeline.id,
        "code": pipeline.code,
        "name": pipeline.name,
        "description": pipeline.description,
        "supported_types": pipeline.supported_types,
        "steps": pipeline.steps,
        "enabled": pipeline.enabled,
        "built_in": False,
        "created_at": pipeline.created_at,
    }


@router.put("/pipelines/{pipeline_id}")
def update_pipeline(
    pipeline_id: str,
    payload: PipelineTemplateUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    pipeline = db.get(PipelineTemplate, pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail="管线不存在")
    if pipeline.code in BUILTIN_PIPELINE_CODES:
        raise HTTPException(status_code=409, detail="内置管线不可修改")
    values = payload.model_dump(exclude_unset=True)
    if values.get("steps") is not None:
        values["steps"] = _validate_pipeline_steps(db, values["steps"])
    for key, value in values.items():
        setattr(pipeline, key, value)
    db.commit()
    db.refresh(pipeline)
    return {
        "id": pipeline.id,
        "code": pipeline.code,
        "name": pipeline.name,
        "description": pipeline.description,
        "supported_types": pipeline.supported_types,
        "steps": pipeline.steps,
        "enabled": pipeline.enabled,
        "built_in": False,
        "created_at": pipeline.created_at,
    }


@router.delete("/pipelines/{pipeline_id}", status_code=204)
def delete_pipeline(
    pipeline_id: str,
    db: Session = Depends(get_db),
) -> Response:
    pipeline = db.get(PipelineTemplate, pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail="管线不存在")
    if pipeline.code in BUILTIN_PIPELINE_CODES:
        raise HTTPException(status_code=409, detail="内置管线不可删除")
    db.delete(pipeline)
    db.commit()
    return Response(status_code=204)


@router.post("/pipelines/{pipeline_id}/run")
def run_pipeline_now(
    pipeline_id: str,
    payload: PipelineRunRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    pipeline = db.get(PipelineTemplate, pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail="管线不存在")
    if not pipeline.enabled:
        raise HTTPException(status_code=409, detail="管线已停用")
    if pipeline.code in BUILTIN_PIPELINE_CODES:
        raise HTTPException(
            status_code=400,
            detail="内置入库管线请通过文件入库流程执行",
        )
    from .mcp_server import _execute_user_pipeline

    execution = PipelineExecution(
        pipeline_code=pipeline.code,
        pipeline_name=pipeline.name,
        space_id=current_space_id(request),
        status="running",
        inputs=payload.inputs,
        step_inputs=payload.step_inputs,
        started_at=utc_now(),
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)

    try:
        results = _execute_user_pipeline(
            pipeline.code,
            {
                "code": pipeline.code,
                "name": pipeline.name,
                "steps": pipeline.steps,
            },
            inputs=payload.inputs,
            step_inputs=payload.step_inputs,
        )
        execution.status = "success"
        execution.results = results
        execution.finished_at = utc_now()
        db.commit()
        return {
            "execution_id": execution.id,
            "pipeline_code": pipeline.code,
            "status": "success",
            "results": results,
        }
    except Exception as error:
        execution.status = "failed"
        execution.error_message = f"{type(error).__name__}: {error}"
        execution.finished_at = utc_now()
        db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"用户管线执行失败：{type(error).__name__}: {error}",
        ) from error
