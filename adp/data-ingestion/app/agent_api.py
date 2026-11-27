from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .agent_attachments import cleanup_agent_attachments, store_agent_attachment
from .agent_runtime import (
    _execute_tool,
    _mcp_session,
    get_session_detail,
    resume_after_approval,
    run_agent_turn,
    stream_agent_turn,
    stream_approval_decision,
)
from .database import current_space_id, get_db
from .config import AUTH_ENABLED
from .model_access import default_model_status
from .models import (
    AgentApproval,
    AgentMemory,
    AgentMessage,
    AgentSession,
    utc_now,
)
from .schemas import (
    AgentApprovalRequest,
    AgentMemoryCreate,
    AgentSessionCreate,
    AgentTurnRequest,
)


router = APIRouter(prefix="/api/agent", tags=["agent"])

STREAM_RESPONSES = {
    200: {
        "description": "Server-sent events containing Agent progress and output",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
}


def stream_events(events):
    async def body():
        async for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        body(),
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        media_type="text/event-stream",
    )


def current_owner(request: Request) -> str:
    caller = getattr(request.state, "caller", None)
    owner_id = str(getattr(caller, "subject", "") or "").strip()
    if not owner_id:
        raise HTTPException(status_code=401, detail="认证身份缺失")
    return owner_id


def current_access_token(request: Request) -> str:
    caller = getattr(request.state, "caller", None)
    access_token = str(getattr(caller, "access_token", "") or "").strip()
    if not access_token:
        if not AUTH_ENABLED:
            return ""
        raise HTTPException(status_code=401, detail="认证令牌缺失")
    return access_token


def owned_session(
    db: Session,
    session_id: str,
    owner_id: str,
) -> AgentSession:
    session = db.get(AgentSession, session_id)
    if session is None or session.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.get("/status")
async def read_agent_status(request: Request) -> dict[str, Any]:
    return await default_model_status(current_access_token(request))


@router.get("/sessions")
def list_agent_sessions(
    request: Request,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    sessions = db.scalars(
        select(AgentSession)
        .where(AgentSession.owner_id == current_owner(request))
        .order_by(AgentSession.updated_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": item.id,
            "title": item.title,
            "status": item.status,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }
        for item in sessions
    ]


@router.post("/sessions")
def create_agent_session(
    payload: AgentSessionCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    session = AgentSession(
        owner_id=current_owner(request),
        title=payload.title.strip() or "新会话",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "id": session.id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


@router.get("/sessions/{session_id}")
def read_agent_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        owned_session(db, session_id, current_owner(request))
        return get_session_detail(db, session_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/sessions/{session_id}")
def delete_agent_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    session = owned_session(db, session_id, current_owner(request))
    if session.status in {"running", "awaiting_approval"}:
        raise HTTPException(status_code=409, detail="会话正在执行或等待审批，暂不能删除")
    attachments = [
        attachment
        for message in session.messages
        for attachment in (message.attachments or [])
    ]
    db.delete(session)
    db.commit()
    cleanup_agent_attachments(attachments)
    return {"deleted": True}


@router.post("/sessions/{session_id}/messages")
async def send_agent_message(
    session_id: str,
    payload: AgentTurnRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        owned_session(db, session_id, current_owner(request))
        return await run_agent_turn(
            session_id,
            current_access_token(request),
            payload.content.strip(),
            current_space_id(request),
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/sessions/{session_id}/messages/stream",
    response_class=StreamingResponse,
    responses=STREAM_RESPONSES,
)
async def send_agent_message_stream(
    session_id: str,
    payload: AgentTurnRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        owned_session(db, session_id, current_owner(request))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return stream_events(
        stream_agent_turn(
            session_id,
            current_access_token(request),
            payload.content.strip(),
            current_space_id(request),
        )
    )


@router.post("/sessions/{session_id}/messages/attachment")
async def send_agent_message_with_attachment(
    session_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    content: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        session = owned_session(db, session_id, current_owner(request))
        if session.status == "awaiting_approval":
            raise RuntimeError("会话正在等待人工审批")
        if session.status == "running":
            raise RuntimeError("会话正在执行，请稍后再发送")
        content = content.strip()
        if len(content) > 50000:
            raise HTTPException(status_code=400, detail="消息内容超过长度限制")
        access_token = current_access_token(request)
        space_id = current_space_id(request)
        attachment = await store_agent_attachment(
            owner_id=session.owner_id,
            session_id=session.id,
            upload=file,
        )
        try:
            return await run_agent_turn(
                session_id,
                access_token,
                content or None,
                space_id,
                attachments=[attachment],
            )
        except Exception:
            cleanup_agent_attachments([attachment])
            raise
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/sessions/{session_id}/messages/attachment/stream",
    response_class=StreamingResponse,
    responses=STREAM_RESPONSES,
)
async def send_agent_message_with_attachment_stream(
    session_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    content: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        session = owned_session(db, session_id, current_owner(request))
        if session.status == "awaiting_approval":
            raise RuntimeError("会话正在等待人工审批")
        if session.status == "running":
            raise RuntimeError("会话正在执行，请稍后再发送")
        content = content.strip()
        if len(content) > 50000:
            raise HTTPException(status_code=400, detail="消息内容超过长度限制")
        attachment = await store_agent_attachment(
            owner_id=session.owner_id,
            session_id=session.id,
            upload=file,
        )
        return stream_events(
            stream_agent_turn(
                session_id,
                current_access_token(request),
                content or None,
                current_space_id(request),
                attachments=[attachment],
            )
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/sessions/{session_id}/approvals/{approval_id}/approve")
async def approve_agent_tool(
    session_id: str,
    approval_id: str,
    payload: AgentApprovalRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    session = owned_session(db, session_id, current_owner(request))
    approval = db.get(AgentApproval, approval_id)
    if approval is None or approval.session_id != session.id:
        raise HTTPException(status_code=404, detail="审批记录不存在")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="该操作已经处理")
    approval.status = "approved"
    approval.reason = payload.reason
    approval.resolved_at = utc_now()
    db.commit()

    try:
        space_id = current_space_id(request)
        async with _mcp_session(space_id) as mcp:
            result = await _execute_tool(
                db,
                session_id,
                mcp,
                approval.tool_name,
                approval.arguments,
            )
        content = json.dumps(result, ensure_ascii=False, default=str)
        approval.result = result
    except Exception as error:
        content = json.dumps(
            {"error": f"{type(error).__name__}: {error}"},
            ensure_ascii=False,
        )
        approval.result = {"error": content}
    approval.status = "approved"
    db.add(
        AgentMessage(
            session_id=session_id,
            role="tool",
            name=approval.tool_name,
            tool_call_id=approval.tool_call_id,
            content=content,
        )
    )
    db.commit()
    return await resume_after_approval(
        session_id,
        current_access_token(request),
        current_space_id(request),
    )


@router.post(
    "/sessions/{session_id}/approvals/{approval_id}/approve/stream",
    response_class=StreamingResponse,
    responses=STREAM_RESPONSES,
)
async def approve_agent_tool_stream(
    session_id: str,
    approval_id: str,
    payload: AgentApprovalRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    session = owned_session(db, session_id, current_owner(request))
    approval = db.get(AgentApproval, approval_id)
    if approval is None or approval.session_id != session.id:
        raise HTTPException(status_code=404, detail="审批记录不存在")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="该操作已经处理")
    return stream_events(
        stream_approval_decision(
            session_id,
            approval_id,
            current_access_token(request),
            current_space_id(request),
            approve=True,
            reason=payload.reason,
            owner_id=current_owner(request),
        )
    )


@router.post("/sessions/{session_id}/approvals/{approval_id}/reject")
async def reject_agent_tool(
    session_id: str,
    approval_id: str,
    payload: AgentApprovalRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    session = owned_session(db, session_id, current_owner(request))
    approval = db.get(AgentApproval, approval_id)
    if approval is None or approval.session_id != session.id:
        raise HTTPException(status_code=404, detail="审批记录不存在")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="该操作已经处理")
    approval.status = "rejected"
    approval.reason = payload.reason or "用户拒绝执行"
    approval.resolved_at = utc_now()
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


@router.post(
    "/sessions/{session_id}/approvals/{approval_id}/reject/stream",
    response_class=StreamingResponse,
    responses=STREAM_RESPONSES,
)
async def reject_agent_tool_stream(
    session_id: str,
    approval_id: str,
    payload: AgentApprovalRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    session = owned_session(db, session_id, current_owner(request))
    approval = db.get(AgentApproval, approval_id)
    if approval is None or approval.session_id != session.id:
        raise HTTPException(status_code=404, detail="审批记录不存在")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="该操作已经处理")
    return stream_events(
        stream_approval_decision(
            session_id,
            approval_id,
            current_access_token(request),
            current_space_id(request),
            approve=False,
            reason=payload.reason,
            owner_id=current_owner(request),
        )
    )
    db.commit()
    return await resume_after_approval(
        session_id,
        current_access_token(request),
        current_space_id(request),
    )


@router.post("/sessions/{session_id}/memories")
def create_agent_memory(
    session_id: str,
    payload: AgentMemoryCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    session = owned_session(db, session_id, current_owner(request))
    memory = db.scalar(
        select(AgentMemory).where(
            AgentMemory.session_id == session.id,
            AgentMemory.memory_key == payload.key,
        )
    )
    if memory is None:
        memory = AgentMemory(
            session_id=session.id,
            memory_key=payload.key,
            value=payload.value,
        )
        db.add(memory)
    else:
        memory.value = payload.value
        memory.updated_at = utc_now()
    db.commit()
    db.refresh(memory)
    return {
        "id": memory.id,
        "memory_key": memory.memory_key,
        "value": memory.value,
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
    }


@router.delete("/sessions/{session_id}/memories/{memory_id}")
def delete_agent_memory(
    session_id: str,
    memory_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    session = owned_session(db, session_id, current_owner(request))
    memory = db.get(AgentMemory, memory_id)
    if memory is None or memory.session_id != session.id:
        raise HTTPException(status_code=404, detail="记忆不存在")
    db.delete(memory)
    db.commit()
    return {"deleted": True}
