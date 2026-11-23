from __future__ import annotations

import io
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from .auth import check_permission, introspect_token
from .config import (
    INTERNAL_SERVICE_TOKEN,
    MCP_ALLOWED_ROOTS,
    MCP_API_URL,
    MCP_AUTH_ENABLED,
    MCP_RESOURCE_URL,
    OAUTH_ISSUER_URL,
)
from .pipeline_runtime import PipelineRuntimeError, execute_pipeline_steps


SERVER_INSTRUCTIONS = """
本服务把多源数据接入平台暴露为 MCP 工具。处理入库请求时，先调用
inspect_local_file 检查文件与推荐管线，再调用 list_datasets 选择目标数据集；
有疑问时优先使用 auto 管线。正式调用 ingest_local_file 后读取返回的
run.id 和 stages，确认 inserted_count、duplicate_count 和每个阶段状态。
工具成功返回代表任务已经完成。不要自行猜测文件路径、数据集 ID 或管线代码。
""".strip()

class OpenBKNTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            subject, client_id = await introspect_token(token)
            if not await check_permission(subject, "mcp_execute"):
                return None
        except Exception:
            return None
        return AccessToken(
            token=token,
            client_id=client_id or subject,
            scopes=["data_ingestion:mcp_execute"],
            subject=subject,
            resource=MCP_RESOURCE_URL,
        )


auth_settings = (
    AuthSettings(
        issuer_url=OAUTH_ISSUER_URL,
        resource_server_url=MCP_RESOURCE_URL,
        validate_token_resource=False,
    )
    if MCP_AUTH_ENABLED
    else None
)

mcp = FastMCP(
    name="marine_ingestion",
    instructions=SERVER_INSTRUCTIONS,
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8010")),
    streamable_http_path=os.getenv("MCP_HTTP_PATH", "/mcp"),
    auth=auth_settings,
    token_verifier=OpenBKNTokenVerifier() if auth_settings else None,
)


def _request(
    method: str,
    path: str,
    *,
    timeout: float = 60.0,
    **kwargs: Any,
) -> Any:
    with httpx.Client(base_url=MCP_API_URL, timeout=timeout) as client:
        headers = dict(kwargs.pop("headers", {}))
        if INTERNAL_SERVICE_TOKEN:
            headers["x-data-ingestion-internal-token"] = INTERNAL_SERVICE_TOKEN
        headers["x-data-ingestion-space"] = os.getenv(
            "DATA_INGESTION_SPACE_ID",
            "default",
        )
        response = client.request(method, path, headers=headers, **kwargs)
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"入库 API 返回 {response.status_code}：{detail}")
    return response.json()


def _allowed_path(file_path: str) -> Path:
    path = Path(file_path).expanduser().resolve(strict=True)
    roots = [
        Path(root).expanduser().resolve()
        for root in MCP_ALLOWED_ROOTS
        if root.strip()
    ]
    if not roots or not any(path.is_relative_to(root) for root in roots):
        raise PermissionError(
            "文件不在 MCP_ALLOWED_ROOTS 允许的目录内："
            + os.pathsep.join(str(root) for root in roots)
        )
    if not path.is_file():
        raise ValueError("file_path 必须指向文件")
    return path


def _inspect_upload(filename: str, content: bytes) -> dict[str, Any]:
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return _request(
        "POST",
        "/api/uploads/inspect",
        files={"file": (filename, content, media_type)},
    )


@mcp.tool()
def api_health() -> dict[str, Any]:
    """检查入库 API 是否可访问。"""
    return _request("GET", "/api/health", timeout=10)


@mcp.tool()
def storage_info() -> dict[str, Any]:
    """返回数据库、对象存储或本地文件存储的实际配置。"""
    return _request("GET", "/api/storage/info", timeout=10)


@mcp.tool()
def list_datasets(query: str = "") -> list[dict[str, Any]]:
    """列出可写入的数据集，可按名称或描述搜索。"""
    return _request(
        "GET",
        "/api/datasets",
        params={"q": query or None},
        timeout=15,
    )


@mcp.tool()
def get_dataset_schema(
    dataset_id: str,
    sample_size: int = 100,
) -> dict[str, Any]:
    """读取指定数据集抽样推断出的字段结构，供下游功能对接。"""
    return _request(
        "GET",
        f"/api/datasets/{dataset_id}/schema",
        params={"sample_size": max(1, min(500, sample_size))},
        timeout=30,
    )


@mcp.tool()
def get_dataset_record_changes(
    dataset_id: str,
    cursor: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """按游标读取新增规范记录；首次调用不传 cursor。"""
    return _request(
        "GET",
        f"/api/datasets/{dataset_id}/records/changes",
        params={
            "cursor": cursor or None,
            "limit": max(1, min(500, limit)),
        },
        timeout=30,
    )


@mcp.tool()
def list_pipeline_templates() -> list[dict[str, Any]]:
    """列出可用入库管线、支持的文件类型和完整阶段。"""
    return _request("GET", "/api/templates", timeout=15)


@mcp.tool()
def inspect_local_file(file_path: str) -> dict[str, Any]:
    """
    预览 MCP 服务所在机器上的文件，不执行入库。
    file_path 必须位于 MCP_ALLOWED_ROOTS 内。
    """
    path = _allowed_path(file_path)
    return _inspect_upload(path.name, path.read_bytes())


@mcp.tool()
def ingest_local_file(
    file_path: str,
    dataset_id: str,
    template_code: str = "auto",
    source_id: str | None = None,
    wait: bool = True,
) -> dict[str, Any]:
    """
    把 MCP 服务所在机器上的文件直接入库。
    先检查文件，template_code=auto 时使用推荐管线；wait=true 时等待全部阶段完成。
    """
    path = _allowed_path(file_path)
    content = path.read_bytes()
    inspection = _inspect_upload(path.name, content)
    resolved_template = (
        inspection["recommended_pipeline"]
        if template_code == "auto"
        else template_code
    )
    media_type = inspection["media_type"] or "application/octet-stream"
    data = {
        "dataset_id": dataset_id,
        "template_code": resolved_template,
    }
    if source_id:
        data["source_id"] = source_id
    run = _request(
        "POST",
        "/api/runs",
        files={"file": (path.name, content, media_type)},
        data=data,
        timeout=300,
    )
    if not wait:
        return run
    return _wait_for_run(run["id"])


@mcp.tool()
def ingest_text(
    content: str,
    filename: str,
    dataset_id: str,
    template_code: str = "auto",
    wait: bool = True,
) -> dict[str, Any]:
    """
    直接入库 Agent 提供的小型文本内容，例如 CSV、JSON、Markdown。
    单次最多 10 MB。
    """
    encoded = content.encode("utf-8")
    if len(encoded) > 10 * 1024 * 1024:
        raise ValueError("ingest_text 单次最多写入 10 MB；大文件请使用 ingest_local_file")
    inspection = _inspect_upload(filename, encoded)
    resolved_template = (
        inspection["recommended_pipeline"]
        if template_code == "auto"
        else template_code
    )
    run = _request(
        "POST",
        "/api/runs",
        files={
            "file": (
                filename,
                io.BytesIO(encoded),
                inspection["media_type"] or "text/plain",
            )
        },
        data={
            "dataset_id": dataset_id,
            "template_code": resolved_template,
        },
        timeout=300,
    )
    if not wait:
        return run
    return _wait_for_run(run["id"])


@mcp.tool()
def get_ingestion_run(run_id: str) -> dict[str, Any]:
    """读取一次入库任务及其七个阶段的详细状态。"""
    return _request("GET", f"/api/runs/{run_id}", timeout=15)


@mcp.tool()
def query_dataset_records(
    dataset_id: str,
    page: int = 1,
    page_size: int = 20,
    query: str = "",
) -> dict[str, Any]:
    """分页读取指定数据集的规范记录，可搜索 JSON 内容。"""
    return _request(
        "GET",
        f"/api/datasets/{dataset_id}/records",
        params={
            "page": max(1, page),
            "page_size": max(1, min(200, page_size)),
            "q": query or None,
        },
        timeout=30,
    )


@mcp.tool()
def list_scan_jobs() -> list[dict[str, Any]]:
    """列出所有定时扫描任务、启用状态和下次执行时间。"""
    return _request("GET", "/api/auto-scan/jobs", timeout=20)


@mcp.tool()
def create_scan_job(
    name: str,
    source_id: str,
    dataset_id: str,
    scan_type: str,
    schedule_type: str = "interval",
    interval_seconds: int = 300,
    schedule_time: str = "02:00",
    weekdays: list[int] | None = None,
    cron_expression: str = "",
    timezone: str = "Asia/Shanghai",
    path: str = "",
    recursive: bool = False,
    file_pattern: str = "*",
    template_code: str = "auto",
) -> dict[str, Any]:
    """创建用户可设定时间的自动扫描任务，支持 interval、daily、weekly、cron。"""
    return _request(
        "POST",
        "/api/auto-scan/jobs",
        json={
            "name": name,
            "source_id": source_id,
            "dataset_id": dataset_id,
            "scan_type": scan_type,
            "schedule_type": schedule_type,
            "interval_seconds": interval_seconds,
            "schedule_time": schedule_time,
            "weekdays": weekdays or [],
            "cron_expression": cron_expression,
            "timezone": timezone,
            "path": path,
            "recursive": recursive,
            "file_pattern": file_pattern,
            "template_code": template_code,
            "enabled": True,
        },
        timeout=30,
    )


@mcp.tool()
def get_scan_job(job_id: str) -> dict[str, Any]:
    """读取定时扫描任务的计划、状态和上次运行结果。"""
    return _request("GET", f"/api/auto-scan/jobs/{job_id}", timeout=20)


@mcp.tool()
def run_scan_job_now(job_id: str) -> dict[str, Any]:
    """立即执行已配置的定时扫描任务，并返回扫描后的状态。"""
    return _request(
        "POST",
        f"/api/auto-scan/jobs/{job_id}/run",
        json={},
        timeout=300,
    )


@mcp.tool()
def capability_catalog() -> dict[str, Any]:
    """读取完整能力目录：内置/自定义 MCP 工具、Skill、管线及详细信息。"""
    return {
        "custom_mcp_tools": _request("GET", "/api/capabilities/mcp-tools", timeout=20),
        "custom_skills": _request("GET", "/api/capabilities/skills", timeout=20),
        "pipelines": _request("GET", "/api/templates", timeout=20),
    }


@mcp.tool()
def create_mcp_tool(
    name: str,
    display_name: str,
    description: str,
    url: str,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    read_only: bool = True,
    requires_approval: bool = True,
) -> dict[str, Any]:
    """创建用户 MCP HTTP 工具。url 必须为 http/https，创建后默认进入可用能力目录。"""
    return _request(
        "POST",
        "/api/capabilities/mcp-tools",
        json={
            "name": name,
            "display_name": display_name,
            "description": description,
            "read_only": read_only,
            "requires_approval": requires_approval,
            "tool_type": "http",
            "config": {
                "url": url,
                "method": method,
                "headers": headers or {},
                "timeout_seconds": 30,
            },
            "enabled": True,
        },
        timeout=30,
    )


@mcp.tool()
def call_custom_mcp_tool(
    tool_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """调用用户创建的 MCP HTTP 工具，并把 arguments 作为查询参数或 JSON 请求体。"""
    return _request(
        "POST",
        f"/api/capabilities/mcp-tools/{tool_id}/execute",
        json={"arguments": arguments},
        timeout=300,
    )


@mcp.tool()
def create_skill(
    name: str,
    display_name: str,
    description: str,
    content: str,
) -> dict[str, Any]:
    """创建用户 Skill。content 使用 Markdown，说明适用场景、步骤和工具约束。"""
    return _request(
        "POST",
        "/api/capabilities/skills",
        json={
            "name": name,
            "display_name": display_name,
            "description": description,
            "content": content,
            "enabled": True,
        },
        timeout=30,
    )


@mcp.tool()
def update_skill(
    skill_id: str,
    content: str,
    display_name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """编辑用户 Skill；内置 Skill 不需要也不允许通过此工具修改。"""
    body: dict[str, Any] = {"content": content}
    if display_name:
        body["display_name"] = display_name
    if description is not None:
        body["description"] = description
    return _request(
        "PUT",
        f"/api/capabilities/skills/{skill_id}",
        json=body,
        timeout=30,
    )


@mcp.tool()
def create_ingestion_pipeline(
    code: str,
    name: str,
    description: str,
    supported_types: list[str],
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    创建用户管线。code 必须以 user- 开头；每步使用 tool_name 和 parameters
    引用内置或用户 MCP 工具。parameters 可包含 ${inputs.x} 与
    ${steps.step-1.result.x} 引用。
    """
    return _request(
        "POST",
        "/api/capabilities/pipelines",
        json={
            "code": code,
            "name": name,
            "description": description,
            "supported_types": supported_types,
            "steps": steps,
            "enabled": True,
        },
        timeout=30,
    )


@mcp.tool()
def run_user_pipeline(
    pipeline_code: str,
    inputs: dict[str, Any] | None = None,
    step_inputs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    执行用户管线。inputs 提供管线级输入，step_inputs 可按步骤序号或 key 覆盖参数。
    步骤参数支持 ${inputs.xxx} 和 ${steps.step-1.result.xxx} 引用。
    """
    pipeline = _request(
        "GET",
        f"/api/templates/{pipeline_code}",
        timeout=20,
    )
    execution = _request(
        "POST",
        "/api/capabilities/pipeline-executions",
        json={
            "pipeline_code": pipeline_code,
            "pipeline_name": pipeline.get("name") or pipeline_code,
            "inputs": inputs or {},
            "step_inputs": step_inputs or {},
        },
        timeout=20,
    )
    try:
        results = _execute_user_pipeline(
            pipeline_code,
            pipeline,
            inputs=inputs,
            step_inputs=step_inputs,
        )
    except Exception as error:
        _request(
            "PATCH",
            f"/api/capabilities/pipeline-executions/{execution['id']}",
            json={
                "status": "failed",
                "error_message": f"{type(error).__name__}: {error}",
            },
            timeout=20,
        )
        raise
    _request(
        "PATCH",
        f"/api/capabilities/pipeline-executions/{execution['id']}",
        json={"status": "success", "results": results},
        timeout=20,
    )
    return {
        "execution_id": execution["id"],
        "pipeline_code": pipeline_code,
        "status": "success",
        "results": results,
    }


def _execute_user_pipeline(
    pipeline_code: str,
    pipeline: dict[str, Any],
    *,
    inputs: dict[str, Any] | None = None,
    step_inputs: dict[str, dict[str, Any]] | None = None,
    on_step_start=None,
    on_step_result=None,
) -> list[dict[str, Any]]:
    def call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        implementation = globals().get(tool_name)
        if tool_name.startswith("_") or (
            not callable(implementation)
            and tool_name not in {
                item["name"]
                for item in _request("GET", "/api/capabilities/mcp-tools", timeout=20)
                if item.get("enabled")
            }
        ):
            raise PipelineRuntimeError(f"管线引用的 MCP 工具不可执行：{tool_name}")
        if callable(implementation):
            return implementation(**arguments)
        custom_tools = _request("GET", "/api/capabilities/mcp-tools", timeout=20)
        custom = next(item for item in custom_tools if item.get("name") == tool_name)
        return call_custom_mcp_tool(custom["id"], arguments)

    return execute_pipeline_steps(
        pipeline.get("steps") or [],
        inputs=inputs,
        step_inputs=step_inputs,
        call_tool=call_tool,
        on_step_start=on_step_start,
        on_step_result=on_step_result,
    )


def _wait_for_run(run_id: str, timeout_seconds: float = 300.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = _request("GET", f"/api/runs/{run_id}", timeout=20)
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.4)
    raise TimeoutError(f"入库任务 {run_id} 在 {timeout_seconds:.0f} 秒内未完成")


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("MCP_TRANSPORT 只支持 stdio 或 streamable-http")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
