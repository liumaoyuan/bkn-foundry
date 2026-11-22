# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

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
