#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
from typing import Any

from fastapi import HTTPException, UploadFile

from .config import MAX_UPLOAD_BYTES, MCP_ALLOWED_ROOTS
from .file_inspector import inspect_file
from .models import new_id


def _attachment_root() -> Path:
    roots = [item.strip() for item in MCP_ALLOWED_ROOTS if item.strip()]
    if not roots:
        raise HTTPException(status_code=500, detail="Agent 附件目录未配置")
    root = Path(roots[0]).expanduser().resolve() / "agent-uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    basename = re.sub(r"[\x00-\x1f\x7f]", "_", basename)
    if not basename or basename in {".", ".."}:
        return "attachment"
    return basename[:180]


async def store_agent_attachment(
    *,
    owner_id: str,
    session_id: str,
    upload: UploadFile,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise HTTPException(status_code=400, detail="会话 ID 无效")

    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="附件超过上传大小限制")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(status_code=400, detail="附件为空")

    filename = _safe_filename(upload.filename or "attachment")
    try:
        inspection = inspect_file(filename, content)
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"附件预览失败：{type(error).__name__}: {error}",
        ) from error

    attachment_id = new_id()
    owner_key = sha256(owner_id.encode("utf-8")).hexdigest()[:16]
    folder = _attachment_root() / owner_key / session_id
    folder.mkdir(parents=True, exist_ok=True)
    stored_path = (folder / f"{attachment_id}-{filename}").resolve()
    root = _attachment_root()
    if not stored_path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="附件路径无效")
    stored_path.write_bytes(content)
    try:
        stored_path.chmod(0o600)
    except OSError:
        pass

    return {
        "id": attachment_id,
        "filename": filename,
        "file_type": str(inspection.get("file_type") or "unknown"),
        "media_type": str(inspection.get("media_type") or "application/octet-stream"),
        "recommended_pipeline": str(inspection.get("recommended_pipeline") or "auto"),
        "sha256": sha256(content).hexdigest(),
        "size": len(content),
        "file_path": str(stored_path),
    }


def cleanup_agent_attachments(attachments: list[dict[str, Any]]) -> None:
    try:
        root = _attachment_root()
    except HTTPException:
        return
    for attachment in attachments:
        raw_path = str(attachment.get("file_path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser().resolve()
        if path.is_relative_to(root):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
