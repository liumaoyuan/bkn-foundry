# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import MODEL_API_URL, MODEL_MANAGER_URL


def auth_headers(access_token: str) -> dict[str, str]:
    headers = {
        "Accept-Language": "zh-CN",
        "Content-Type": "application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


async def default_model_status(access_token: str) -> dict[str, Any]:
    headers = auth_headers(access_token)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5, connect=3)
        ) as client:
            response = await client.get(
                f"{MODEL_MANAGER_URL}/api/mf-model-manager/v1/llm/list",
                params={
                    "page": 1,
                    "size": 100,
                    "order": "desc",
                    "rule": "update_time",
                    "series": "all",
                    "name": "",
                    "api_model": "",
                },
                headers=headers,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"model manager returned HTTP {response.status_code}"
            )
        payload = response.json()
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise RuntimeError("model manager returned an invalid response")
        default = next(
            (
                item
                for item in models
                if isinstance(item, dict)
                and (item.get("f_default") == 1 or item.get("default") is True)
            ),
            None,
        )
        return {
            "configured": default is not None,
            "managed_by": "model-resources",
            "default_model_id": str(default.get("model_id") or "") if default else "",
            "default_model_name": str(default.get("model_name") or "") if default else "",
        }
    except Exception as error:
        return {
            "configured": False,
            "managed_by": "model-resources",
            "default_model_id": "",
            "default_model_name": "",
            "error": f"{type(error).__name__}: {error}",
        }


async def post_chat_completion(
    access_token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            MODEL_API_URL,
            headers=auth_headers(access_token),
            json=payload,
        )
    if response.is_error:
        try:
            detail: Any = response.json()
        except ValueError:
            detail = response.text
        raise RuntimeError(
            f"model API returned HTTP {response.status_code}: {detail}"
        )
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("model API returned an invalid response")
    return data


def _content_delta(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _merge_tool_call_delta(
    tool_calls: dict[int, dict[str, Any]],
    delta: dict[str, Any],
) -> None:
    index = int(delta.get("index", len(tool_calls)))
    current = tool_calls.setdefault(
        index,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if isinstance(delta.get("id"), str):
        current["id"] = delta["id"]
    if isinstance(delta.get("type"), str):
        current["type"] = delta["type"]
    function = delta.get("function")
    if not isinstance(function, dict):
        return
    target = current["function"]
    if isinstance(function.get("name"), str):
        target["name"] += function["name"]
    if isinstance(function.get("arguments"), str):
        target["arguments"] += function["arguments"]


async def stream_chat_completion(
    access_token: str,
    payload: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    request_payload = {**payload, "stream": True}
    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        async with client.stream(
            "POST",
            MODEL_API_URL,
            headers=auth_headers(access_token),
            json=request_payload,
        ) as response:
            if response.is_error:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"model API returned HTTP {response.status_code}: {body}"
                )

            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                body = await response.aread()
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise RuntimeError("model API returned an invalid response")
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = _content_delta(message.get("content"))
                if content:
                    yield {"type": "content_delta", "content": content}
                yield {"type": "completed", "response": data}
                return

            saw_data = False
            async for line in response.aiter_lines():
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                saw_data = True
                data_text = stripped[5:].strip()
                if not data_text or data_text == "[DONE]":
                    continue
                data = json.loads(data_text)
                if not isinstance(data, dict):
                    continue
                choice = (data.get("choices") or [{}])[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}

                content = _content_delta(delta.get("content"))
                if content:
                    content_parts.append(content)
                    yield {"type": "content_delta", "content": content}

                reasoning = _content_delta(
                    delta.get("reasoning_content") or delta.get("reasoning")
                )
                if reasoning:
                    yield {"type": "reasoning_delta", "content": reasoning}

                for tool_delta in delta.get("tool_calls") or []:
                    if isinstance(tool_delta, dict):
                        _merge_tool_call_delta(tool_calls, tool_delta)

            if not saw_data:
                raise RuntimeError("model API returned an empty stream")

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    merged_tool_calls = [tool_calls[index] for index in sorted(tool_calls)]
    if merged_tool_calls:
        message["tool_calls"] = merged_tool_calls
    yield {
        "type": "completed",
        "response": {
            "choices": [
                {
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ]
        },
    }
