from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .agent_catalog import get_skill_content
from .config import (
    AGENT_HISTORY_LIMIT,
    AGENT_MAX_TOOL_ROUNDS,
    AGENT_SELF_API_URL,
    MCP_ALLOWED_ROOTS,
)
from .model_access import post_chat_completion, stream_chat_completion
from .models import (
    AgentApproval,
    AgentMemory,
    AgentMessage,
    AgentSession,
    Dataset,
    utc_now,
)


WRITE_TOOLS = {
    "ingest_local_file",
    "ingest_text",
    "create_scan_job",
    "run_scan_job_now",
    "create_mcp_tool",
    "call_custom_mcp_tool",
    "create_skill",
    "update_skill",
    "create_ingestion_pipeline",
    "run_user_pipeline",
}
LOCAL_TOOL_NAMES = {"remember_memory", "recall_memory"}
BACKEND_DIR = Path(__file__).resolve().parents[1]

LOCAL_TOOL_DEFINITIONS = [
    {
        "name": "remember_memory",
        "description": "保存用户明确要求记住的长期会话事实。不要自动保存临时信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "简短稳定的记忆键"},
                "value": {"type": "string", "description": "要记住的内容"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall_memory",
        "description": "读取当前会话已经保存的长期记忆。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@asynccontextmanager
async def _mcp_session(space_id: str = "default"):
    environment = {
        **os.environ,
        "MCP_API_URL": AGENT_SELF_API_URL,
        "MCP_ALLOWED_ROOTS": os.pathsep.join(MCP_ALLOWED_ROOTS),
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONUTF8": "1",
        "DATA_INGESTION_SPACE_ID": space_id,
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=BACKEND_DIR,
        env=environment,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


def _tool_definition(tool) -> dict[str, Any]:
    schema = tool.inputSchema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        },
    }


async def _model_tools(mcp: ClientSession) -> list[dict[str, Any]]:
    mcp_tools = await mcp.list_tools()
    tools = [_tool_definition(tool) for tool in mcp_tools.tools]
    tools.extend(
        {
            "type": "function",
            "function": {
                "name": item["name"],
                "description": item["description"],
                "parameters": item["inputSchema"],
            },
        }
        for item in LOCAL_TOOL_DEFINITIONS
    )
    return tools


def _bounded_tool_result(name: str, value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if name in {"ingest_local_file", "ingest_text", "get_ingestion_run"}:
        stages = value.get("stages") if isinstance(value.get("stages"), list) else []
        return {
            "id": value.get("id"),
            "dataset_id": value.get("dataset_id"),
            "template_code": value.get("template_code"),
            "status": value.get("status"),
            "original_name": value.get("original_name"),
            "inserted_count": value.get("inserted_count"),
            "updated_count": value.get("updated_count"),
            "duplicate_count": value.get("duplicate_count"),
            "error_count": value.get("error_count"),
            "error_message": value.get("error_message"),
            "fingerprint": value.get("fingerprint"),
            "stages": [
                {
                    "sequence": stage.get("sequence"),
                    "name": stage.get("name"),
                    "status": stage.get("status"),
                    "input_count": stage.get("input_count"),
                    "output_count": stage.get("output_count"),
                    "summary": stage.get("summary"),
                }
                for stage in stages
            ],
        }
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > 20000:
        return {"summary": text[:20000], "truncated": True}
    return value


async def _execute_tool(
    db: Session,
    session_id: str,
    mcp: ClientSession,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if name == "remember_memory":
        key = str(arguments.get("key", "")).strip()
        value = str(arguments.get("value", "")).strip()
        if not key or not value:
            raise ValueError("key 和 value 不能为空")
        memory = db.scalar(
            select(AgentMemory).where(
                AgentMemory.session_id == session_id,
                AgentMemory.memory_key == key,
            )
        )
        if memory:
            memory.value = value
        else:
            db.add(
                AgentMemory(
                    session_id=session_id,
                    memory_key=key,
                    value=value,
                )
            )
        db.commit()
        return {"saved": True, "key": key, "value": value}

    if name == "recall_memory":
        memories = db.scalars(
            select(AgentMemory)
            .where(AgentMemory.session_id == session_id)
            .order_by(AgentMemory.updated_at.asc())
        ).all()
        return {
            "memories": [
                {"key": item.memory_key, "value": item.value}
                for item in memories
            ]
        }

    result = await mcp.call_tool(name, arguments)
    payload: Any
    if result.structuredContent is not None:
        payload = result.structuredContent
    else:
        texts = [
            item.text
            for item in result.content
            if getattr(item, "type", "") == "text"
        ]
        if len(texts) == 1:
            try:
                payload = json.loads(texts[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = texts[0]
        else:
            payload = texts
    if result.isError:
        raise RuntimeError(f"工具 {name} 执行失败：{payload}")
    return _bounded_tool_result(name, payload)


def _history_messages(db: Session, session_id: str) -> list[dict[str, Any]]:
    messages = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.session_id == session_id)
        .order_by(AgentMessage.created_at.desc())
        .limit(AGENT_HISTORY_LIMIT)
    ).all()
    messages.reverse()
    while messages and messages[0].role == "tool":
        messages.pop(0)
    result: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role}
        content = message.content or ""
        attachments = message.attachments or []
        if attachments:
            attachment_context = "\n".join(
                (
                    f"- id={attachment.get('id', '')} "
                    f"filename={attachment.get('filename', '')} "
                    f"type={attachment.get('file_type', '')} "
                    f"size={attachment.get('size', 0)} "
                    f"file_path={attachment.get('file_path', '')}"
                )
                for attachment in attachments
            )
            content = (
                f"{content}\n\n[用户上传附件]\n{attachment_context}".strip()
            )
        item["content"] = content
        if message.tool_calls:
            item["tool_calls"] = message.tool_calls
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.name:
            item["name"] = message.name
        result.append(item)
    return result


def _system_prompt(db: Session, session_id: str) -> str:
    skill_bundle = get_skill_content(db)
    skill = skill_bundle["content"]
    custom_skills = "\n\n".join(
        f"### {item['display_name']} ({item['name']})\n{item['content']}"
        for item in skill_bundle.get("custom", [])
        if item.get("enabled")
    ) or "（无用户自定义 Skill）"
    memories = db.scalars(
        select(AgentMemory)
        .where(AgentMemory.session_id == session_id)
        .order_by(AgentMemory.updated_at.asc())
    ).all()
    memory_text = "\n".join(
        f"- {item.memory_key}: {item.value}" for item in memories
    ) or "（暂无）"
    return f"""
你是“多源数据入库 Agent”。你在受约束的 Harness Runtime 中工作。

工程约束：
1. 所有数据操作必须通过 MCP 工具完成，不能直接写数据库或文件系统。
2. 写入工具 ingest_local_file 和 ingest_text 必须先请求人工批准；系统会自动中断。
3. 不得创建不存在的数据集，不得猜测 dataset_id，不得修改表结构。
4. 先 inspect_local_file，再 ingest_local_file；结束后核验阶段、新增数和重复数。
5. 返回结果保持简洁，说明 run_id、状态、写入数、重复数和失败阶段。
6. 只有用户明确要求记住时才调用 remember_memory。会话已保存记忆会自动加入上下文。
7. 用户消息中的“[用户上传附件]”表示用户已经附加文件。处理附件时先对附件 file_path 调用 inspect_local_file；如果用户没有说明目标数据集，先询问用户，不得猜测 dataset_id。

当前会话长期记忆：
{memory_text}

Agent Skill：
{skill}

用户自定义 Skill：
{custom_skills}
""".strip()


def _user_names_dataset(db: Session, content: str) -> bool:
    normalized = " ".join(content.casefold().split())
    if not normalized:
        return False
    datasets = db.scalars(select(Dataset)).all()
    for dataset in datasets:
        dataset_name = " ".join(dataset.name.casefold().split())
        if dataset.id.casefold() in normalized:
            return True
        if len(dataset_name) >= 2 and dataset_name in normalized:
            return True
    return False


def _requires_dataset_clarification(
    db: Session,
    content: str,
    attachments: list[dict[str, Any]] | None,
) -> bool:
    return bool(attachments) and not _user_names_dataset(db, content)


def _normalize_tool_calls(tool_calls: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for call in tool_calls:
        if hasattr(call, "model_dump"):
            item = call.model_dump()
        elif isinstance(call, dict):
            item = call
        else:
            item = {
                "id": getattr(call, "id", ""),
                "type": getattr(call, "type", "function"),
                "function": {
                    "name": getattr(call.function, "name", ""),
                    "arguments": getattr(call.function, "arguments", "{}"),
                },
            }
        normalized.append(item)
    return normalized


async def run_agent_turn(
    session_id: str,
    access_token: str,
    user_content: str | None = None,
    space_id: str = "default",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from .database import session_for_space

    db = session_for_space(space_id)
    try:
        session = db.get(AgentSession, session_id)
        if session is None:
            raise LookupError("会话不存在")
        if session.status == "awaiting_approval":
            raise RuntimeError("会话正在等待人工审批")
        if session.status == "running":
            raise RuntimeError("会话正在执行，请稍后再发送")
        if user_content or attachments:
            if session.title == "新会话":
                title_seed = (
                    " ".join(user_content.split())
                    if user_content
                    else f"附件：{attachments[0].get('filename', '')}"
                )
                session.title = title_seed[:50]
            db.add(
                AgentMessage(
                    session_id=session.id,
                    role="user",
                    content=user_content or "",
                    attachments=attachments or [],
                )
            )
            session.updated_at = utc_now()
            db.commit()
        session.status = "running"
        db.commit()
        requires_dataset_clarification = _requires_dataset_clarification(
            db,
            user_content or "",
            attachments,
        )
        return await _run_model_loop(
            db,
            session,
            access_token,
            space_id,
            requires_dataset_clarification=requires_dataset_clarification,
        )
    finally:
        db.close()


async def resume_after_approval(
    session_id: str,
    access_token: str,
    space_id: str = "default",
) -> dict[str, Any]:
    from .database import session_for_space

    db = session_for_space(space_id)
    try:
        session = db.get(AgentSession, session_id)
        if session is None:
            raise LookupError("会话不存在")
        pending = db.scalar(
            select(AgentApproval.id).where(
                AgentApproval.session_id == session_id,
                AgentApproval.status == "pending",
            )
        )
        if pending:
            return get_session_detail(db, session_id)
        session.status = "running"
        db.commit()
        return await _run_model_loop(db, session, access_token, space_id)
    finally:
        db.close()


async def _run_model_loop(
    db: Session,
    session: AgentSession,
    access_token: str,
    space_id: str = "default",
    requires_dataset_clarification: bool = False,
) -> dict[str, Any]:
    history = _history_messages(db, session.id)
    messages = [
        {"role": "system", "content": _system_prompt(db, session.id)},
        *history,
    ]
    if requires_dataset_clarification:
        messages.append(
            {
                "role": "system",
                "content": (
                    "本轮用户上传了附件，但没有明确指出目标数据集。"
                    "必须先调用 list_datasets 获取可用数据集，并向用户询问要写入哪一个；"
                    "在用户明确选择前不得调用 ingest_local_file 或 ingest_text。"
                ),
            }
        )
    try:
        async with _mcp_session(space_id) as mcp:
            tools = await _model_tools(mcp)
            for _round in range(AGENT_MAX_TOOL_ROUNDS):
                payload = {
                    "model": "",
                    "model_id": "",
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                }
                response = await post_chat_completion(access_token, payload)
                choice = (response.get("choices") or [{}])[0]
                assistant = choice.get("message") or {}
                content = assistant.get("content") or ""
                tool_calls = _normalize_tool_calls(assistant.get("tool_calls") or [])
                assistant_message = AgentMessage(
                    session_id=session.id,
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                )
                db.add(assistant_message)
                session.updated_at = utc_now()
                db.commit()
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                        **({"tool_calls": tool_calls} if tool_calls else {}),
                    }
                )

                if not tool_calls:
                    session.status = "idle"
                    db.commit()
                    return get_session_detail(db, session.id)

                approvals = []
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = str(function.get("name", ""))
                    raw_arguments = function.get("arguments") or "{}"
                    try:
                        arguments = (
                            json.loads(raw_arguments)
                            if isinstance(raw_arguments, str)
                            else raw_arguments
                        )
                    except json.JSONDecodeError:
                        arguments = {"raw": raw_arguments}
                    if (
                        requires_dataset_clarification
                        and name in {"ingest_local_file", "ingest_text"}
                    ):
                        content_text = json.dumps(
                            {
                                "error": (
                                    "用户尚未指定目标数据集。请先向用户说明可选数据集，"
                                    "得到明确选择后再入库。"
                                )
                            },
                            ensure_ascii=False,
                        )
                        db.add(
                            AgentMessage(
                                session_id=session.id,
                                role="tool",
                                name=name,
                                tool_call_id=str(call.get("id", "")),
                                content=content_text,
                            )
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "name": name,
                                "tool_call_id": str(call.get("id", "")),
                                "content": content_text,
                            }
                        )
                        continue
                    if name in WRITE_TOOLS:
                        approval = AgentApproval(
                            session_id=session.id,
                            message_id=assistant_message.id,
                            tool_call_id=str(call.get("id", "")),
                            tool_name=name,
                            arguments=arguments,
                            status="pending",
                            reason="写入数据库属于需要人工确认的操作",
                        )
                        db.add(approval)
                        approvals.append(approval)
                        continue
                    try:
                        tool_result = await _execute_tool(
                            db,
                            session.id,
                            mcp,
                            name,
                            arguments,
                        )
                        content_text = json.dumps(
                            tool_result,
                            ensure_ascii=False,
                            default=str,
                        )
                    except Exception as error:
                        content_text = json.dumps(
                            {"error": f"{type(error).__name__}: {error}"},
                            ensure_ascii=False,
                        )
                    tool_message = AgentMessage(
                        session_id=session.id,
                        role="tool",
                        name=name,
                        tool_call_id=str(call.get("id", "")),
                        content=content_text,
                    )
                    db.add(tool_message)
                    messages.append(
                        {
                            "role": "tool",
                            "name": name,
                            "tool_call_id": str(call.get("id", "")),
                            "content": content_text,
                        }
                    )
                db.commit()
                if approvals:
                    session.status = "awaiting_approval"
                    db.commit()
                    return get_session_detail(db, session.id)

            raise RuntimeError("Agent 超过最大工具调用轮次")
    except Exception as error:
        db.rollback()
        session = db.get(AgentSession, session.id)
        if session:
            session.status = "failed"
        db.add(
            AgentMessage(
                session_id=session.id,
                role="assistant",
                content=f"执行失败：{type(error).__name__}: {error}",
            )
        )
        db.commit()
        return get_session_detail(db, session.id)


async def _stream_model_loop(
    db: Session,
    session: AgentSession,
    access_token: str,
    space_id: str = "default",
    requires_dataset_clarification: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    history = _history_messages(db, session.id)
    messages = [
        {"role": "system", "content": _system_prompt(db, session.id)},
        *history,
    ]
    if requires_dataset_clarification:
        messages.append(
            {
                "role": "system",
                "content": (
                    "本轮用户上传了附件，但没有明确指出目标数据集。"
                    "必须先调用 list_datasets 获取可用数据集，并向用户询问要写入哪一个；"
                    "在用户明确选择前不得调用 ingest_local_file 或 ingest_text。"
                ),
            }
        )
    try:
        async with _mcp_session(space_id) as mcp:
            tools = await _model_tools(mcp)
            for round_index in range(AGENT_MAX_TOOL_ROUNDS):
                payload = {
                    "model": "",
                    "model_id": "",
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                }
                yield {"type": "assistant_start", "round": round_index}
                completed_response: dict[str, Any] | None = None
                async for model_event in stream_chat_completion(access_token, payload):
                    if model_event.get("type") in {
                        "content_delta",
                        "reasoning_delta",
                    }:
                        yield {**model_event, "round": round_index}
                    elif model_event.get("type") == "completed":
                        response = model_event.get("response")
                        if isinstance(response, dict):
                            completed_response = response
                if completed_response is None:
                    raise RuntimeError("模型流没有返回完整响应")

                choice = (completed_response.get("choices") or [{}])[0]
                assistant = choice.get("message") or {}
                content = str(assistant.get("content") or "")
                tool_calls = _normalize_tool_calls(assistant.get("tool_calls") or [])
                assistant_message = AgentMessage(
                    session_id=session.id,
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                )
                db.add(assistant_message)
                session.updated_at = utc_now()
                db.commit()
                yield {
                    "type": "assistant_message",
                    "round": round_index,
                    "message": {
                        "id": assistant_message.id,
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls,
                    },
                }
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                        **({"tool_calls": tool_calls} if tool_calls else {}),
                    }
                )

                if not tool_calls:
                    session.status = "idle"
                    db.commit()
                    yield {"type": "status", "status": "idle"}
                    yield {
                        "type": "done",
                        "session": get_session_detail(db, session.id),
                    }
                    return

                approvals = []
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = str(function.get("name", ""))
                    raw_arguments = function.get("arguments") or "{}"
                    try:
                        arguments = (
                            json.loads(raw_arguments)
                            if isinstance(raw_arguments, str)
                            else raw_arguments
                        )
                    except json.JSONDecodeError:
                        arguments = {"raw": raw_arguments}
                    if (
                        requires_dataset_clarification
                        and name in {"ingest_local_file", "ingest_text"}
                    ):
                        content_text = json.dumps(
                            {
                                "error": (
                                    "用户尚未指定目标数据集。请先向用户说明可选数据集，"
                                    "得到明确选择后再入库。"
                                )
                            },
                            ensure_ascii=False,
                        )
                        db.add(
                            AgentMessage(
                                session_id=session.id,
                                role="tool",
                                name=name,
                                tool_call_id=str(call.get("id", "")),
                                content=content_text,
                            )
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "name": name,
                                "tool_call_id": str(call.get("id", "")),
                                "content": content_text,
                            }
                        )
                        yield {
                            "type": "tool_result",
                            "name": name,
                            "result": {"error": content_text},
                        }
                        continue
                    if name in WRITE_TOOLS:
                        approval = AgentApproval(
                            session_id=session.id,
                            message_id=assistant_message.id,
                            tool_call_id=str(call.get("id", "")),
                            tool_name=name,
                            arguments=arguments,
                            status="pending",
                            reason="写入数据库属于需要人工确认的操作",
                        )
                        db.add(approval)
                        approvals.append(approval)
                        continue

                    yield {
                        "type": "tool_start",
                        "name": name,
                        "arguments": arguments,
                    }
                    try:
                        tool_result = await _execute_tool(
                            db,
                            session.id,
                            mcp,
                            name,
                            arguments,
                        )
                        bounded_result = _bounded_tool_result(name, tool_result)
                        content_text = json.dumps(
                            bounded_result,
                            ensure_ascii=False,
                            default=str,
                        )
                        yield {
                            "type": "tool_result",
                            "name": name,
                            "result": bounded_result,
                        }
                    except Exception as error:
                        content_text = json.dumps(
                            {"error": f"{type(error).__name__}: {error}"},
                            ensure_ascii=False,
                        )
                        yield {
                            "type": "tool_result",
                            "name": name,
                            "result": {"error": content_text},
                        }
                    db.add(
                        AgentMessage(
                            session_id=session.id,
                            role="tool",
                            name=name,
                            tool_call_id=str(call.get("id", "")),
                            content=content_text,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "name": name,
                            "tool_call_id": str(call.get("id", "")),
                            "content": content_text,
                        }
                    )
                db.commit()
                if approvals:
                    session.status = "awaiting_approval"
                    db.commit()
                    yield {
                        "type": "approvals_created",
                        "approvals": [
                            {
                                "id": approval.id,
                                "tool_name": approval.tool_name,
                                "arguments": approval.arguments,
                            }
                            for approval in approvals
                        ],
                    }
                    yield {"type": "status", "status": "awaiting_approval"}
                    yield {
                        "type": "done",
                        "session": get_session_detail(db, session.id),
                    }
                    return

            raise RuntimeError("Agent 超过最大工具调用轮次")
    except Exception as error:
        db.rollback()
        session = db.get(AgentSession, session.id)
        if session:
            session.status = "failed"
        error_text = f"执行失败：{type(error).__name__}: {error}"
        db.add(
            AgentMessage(
                session_id=session.id,
                role="assistant",
                content=error_text,
            )
        )
        db.commit()
        yield {
            "type": "error",
            "message": error_text,
            "session": get_session_detail(db, session.id),
        }


async def stream_agent_turn(
    session_id: str,
    access_token: str,
    user_content: str | None = None,
    space_id: str = "default",
    attachments: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .database import session_for_space

    db = session_for_space(space_id)
    session = db.get(AgentSession, session_id)
    try:
        if session is None:
            raise LookupError("会话不存在")
        if session.status == "awaiting_approval":
            raise RuntimeError("会话正在等待人工审批")
        if session.status == "running":
            raise RuntimeError("会话正在执行，请稍后再发送")
        if user_content or attachments:
            if session.title == "新会话":
                title_seed = (
                    " ".join(user_content.split())
                    if user_content
                    else f"附件：{attachments[0].get('filename', '')}"
                )
                session.title = title_seed[:50]
            message = AgentMessage(
                session_id=session.id,
                role="user",
                content=user_content
                or (
                    f"已附加文件：{attachments[0].get('filename', '')}"
                    if attachments
                    else ""
                ),
                attachments=attachments or [],
            )
            db.add(message)
            session.status = "running"
            session.updated_at = utc_now()
            db.commit()
            yield {
                "type": "user_message",
                "message_id": message.id,
                "session": get_session_detail(db, session.id),
            }
        requires_dataset_clarification = _requires_dataset_clarification(
            db,
            user_content or "",
            attachments,
        )
        yield {"type": "status", "status": "running"}
        async for event in _stream_model_loop(
            db,
            session,
            access_token,
            space_id,
            requires_dataset_clarification=requires_dataset_clarification,
        ):
            yield event
    except Exception as error:
        db.rollback()
        session = db.get(AgentSession, session_id)
        if session:
            session.status = "failed"
            db.add(
                AgentMessage(
                    session_id=session.id,
                    role="assistant",
                    content=f"执行失败：{type(error).__name__}: {error}",
                )
            )
            db.commit()
        yield {
            "type": "error",
            "message": str(error),
            "session": get_session_detail(db, session_id) if session else None,
        }
    finally:
        db.close()


async def stream_approval_decision(
    session_id: str,
    approval_id: str,
    access_token: str,
    space_id: str,
    approve: bool,
    reason: str = "",
    owner_id: str = "",
) -> AsyncIterator[dict[str, Any]]:
    from .database import session_for_space

    db = session_for_space(space_id)
    session = db.get(AgentSession, session_id)
    approval = db.get(AgentApproval, approval_id)
    try:
        if session is None or (owner_id and session.owner_id != owner_id):
            raise LookupError("会话不存在")
        if approval is None or approval.session_id != session.id:
            raise LookupError("审批记录不存在")
        if approval.status != "pending":
            raise RuntimeError("该操作已经处理")

        next_status = "approved" if approve else "rejected"
        approval.status = next_status
        approval.reason = (
            reason
            if approve
            else reason or "用户拒绝执行"
        )
        approval.resolved_at = utc_now()
        db.commit()
        yield {
            "type": "approval_resolved",
            "approval_id": approval.id,
            "status": next_status,
        }

        if approve:
            yield {
                "type": "tool_start",
                "name": approval.tool_name,
                "arguments": approval.arguments,
            }
            try:
                async with _mcp_session(space_id) as mcp:
                    result = await _execute_tool(
                        db,
                        session_id,
                        mcp,
                        approval.tool_name,
                        approval.arguments,
                    )
                approval.result = result
                content = json.dumps(result, ensure_ascii=False, default=str)
                yield {
                    "type": "tool_result",
                    "name": approval.tool_name,
                    "result": _bounded_tool_result(approval.tool_name, result),
                }
            except Exception as error:
                content = json.dumps(
                    {"error": f"{type(error).__name__}: {error}"},
                    ensure_ascii=False,
                )
                approval.result = {"error": content}
                yield {
                    "type": "tool_result",
                    "name": approval.tool_name,
                    "result": {"error": content},
                }
            db.add(
                AgentMessage(
                    session_id=session_id,
                    role="tool",
                    name=approval.tool_name,
                    tool_call_id=approval.tool_call_id,
                    content=content,
                )
            )
        else:
            result = {"error": "用户拒绝执行该写入工具"}
            approval.result = result
            db.add(
                AgentMessage(
                    session_id=session_id,
                    role="tool",
                    name=approval.tool_name,
                    tool_call_id=approval.tool_call_id,
                    content=json.dumps(result, ensure_ascii=False),
                )
            )
        db.commit()

        pending = db.scalar(
            select(AgentApproval.id).where(
                AgentApproval.session_id == session_id,
                AgentApproval.status == "pending",
            )
        )
        if pending:
            session.status = "awaiting_approval"
            db.commit()
            yield {"type": "status", "status": "awaiting_approval"}
            yield {
                "type": "done",
                "session": get_session_detail(db, session_id),
            }
            return

        session.status = "running"
        db.commit()
        yield {"type": "status", "status": "running"}
        async for event in _stream_model_loop(
            db,
            session,
            access_token,
            space_id,
        ):
            yield event
    except Exception as error:
        db.rollback()
        session = db.get(AgentSession, session_id)
        if session:
            session.status = "failed"
            db.add(
                AgentMessage(
                    session_id=session.id,
                    role="assistant",
                    content=f"审批恢复失败：{type(error).__name__}: {error}",
                )
            )
            db.commit()
        yield {
            "type": "error",
            "message": str(error),
            "session": get_session_detail(db, session_id) if session else None,
        }
    finally:
        db.close()


def get_session_detail(db: Session, session_id: str) -> dict[str, Any]:
    db.expire_all()
    session = db.scalar(
        select(AgentSession)
        .where(AgentSession.id == session_id)
        .options(
            selectinload(AgentSession.messages),
            selectinload(AgentSession.approvals),
            selectinload(AgentSession.memories),
        )
    )
    if session is None:
        raise LookupError("会话不存在")
    return {
        "id": session.id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "attachments": message.attachments or [],
                "tool_calls": message.tool_calls,
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "created_at": message.created_at.isoformat(),
            }
            for message in session.messages
        ],
        "approvals": [
            {
                "id": approval.id,
                "session_id": approval.session_id,
                "message_id": approval.message_id,
                "tool_call_id": approval.tool_call_id,
                "tool_name": approval.tool_name,
                "arguments": approval.arguments,
                "status": approval.status,
                "reason": approval.reason,
                "result": approval.result,
                "created_at": approval.created_at.isoformat(),
                "resolved_at": (
                    approval.resolved_at.isoformat()
                    if approval.resolved_at
                    else None
                ),
            }
            for approval in session.approvals
        ],
        "memories": [
            {
                "id": memory.id,
                "memory_key": memory.memory_key,
                "value": memory.value,
                "created_at": memory.created_at.isoformat(),
                "updated_at": memory.updated_at.isoformat(),
            }
            for memory in session.memories
        ],
    }
