from __future__ import annotations

import json
import os
import sys
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
from .model_access import post_chat_completion
from .models import (
    AgentApproval,
    AgentMemory,
    AgentMessage,
    AgentSession,
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
    result: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role}
        if message.content:
            item["content"] = message.content
        else:
            item["content"] = ""
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

当前会话长期记忆：
{memory_text}

Agent Skill：
{skill}

用户自定义 Skill：
{custom_skills}
""".strip()


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
) -> dict[str, Any]:
    from .database import session_for_space

    db = session_for_space(space_id)
    try:
        session = db.get(AgentSession, session_id)
        if session is None:
            raise LookupError("会话不存在")
        if session.status == "awaiting_approval":
            raise RuntimeError("会话正在等待人工审批")
        if user_content:
            if session.title == "新会话":
                session.title = " ".join(user_content.split())[:50]
            db.add(
                AgentMessage(
                    session_id=session.id,
                    role="user",
                    content=user_content,
                )
            )
            session.updated_at = utc_now()
            db.commit()
        session.status = "running"
        db.commit()
        return await _run_model_loop(db, session, access_token, space_id)
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
) -> dict[str, Any]:
    history = _history_messages(db, session.id)
    messages = [
        {"role": "system", "content": _system_prompt(db, session.id)},
        *history,
    ]
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


def get_session_detail(db: Session, session_id: str) -> dict[str, Any]:
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
