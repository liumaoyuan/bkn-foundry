# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

from dataclasses import dataclass
from secrets import compare_digest
from uuid import uuid4

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import (
    AUTH_ENABLED,
    AUTHZ_RESOURCE_ID,
    AUTHZ_RESOURCE_TYPE,
    APP_ENV,
    BKN_SAFE_URL,
    HYDRA_ADMIN_URL,
    INTERNAL_SERVICE_TOKEN,
)


PUBLIC_PATHS = {"/api/health"}
if APP_ENV != "production":
    PUBLIC_PATHS.update({"/docs", "/openapi.json", "/redoc"})


@dataclass(frozen=True)
class CallerIdentity:
    subject: str
    client_id: str
    account_type: str
    trace_id: str
    access_token: str = ""


def error_response(
    status_code: int,
    error_code: str,
    message: str,
    trace_id: str,
    *,
    details: list[dict[str, str]] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "error_code": error_code,
        "message": message,
        "trace_id": trace_id,
    }
    if details:
        body["details"] = details
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={"x-trace-id": trace_id, "x-request-id": trace_id},
    )


def required_operation(request: Request) -> str:
    path = request.url.path
    method = request.method.upper()
    if path == "/api/spaces":
        return "view" if method == "GET" else "task_manage"
    if path.startswith("/api/spaces/") and path.endswith("/test"):
        return "task_manage"
    if path.startswith("/api/agent"):
        return "agent_use"
    if path.startswith("/api/auto-scan/jobs"):
        return "view" if method == "GET" else "task_manage"
    if path == "/api/runs" and method == "POST":
        return "create"
    if path.startswith("/api/runs/") and path.endswith("/retry"):
        return "task_manage"
    if path.startswith("/api/uploads"):
        return "create"
    if path.startswith("/api/sources") and method == "POST":
        return "modify" if path != "/api/sources" else "create"
    if path == "/api/datasets" and method == "POST":
        return "create"
    return "view"


async def introspect_token(token: str) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{HYDRA_ADMIN_URL}/admin/oauth2/introspect",
            data={"token": token},
        )
    if response.status_code != 200:
        raise RuntimeError(f"Hydra introspection failed with HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict) or data.get("active") is not True:
        raise PermissionError("access token is inactive")
    subject = str(data.get("sub") or "").strip()
    if not subject or subject.lower() in {"anonymous", "anonymous-user"}:
        raise PermissionError("access token has no usable subject")
    return subject, str(data.get("client_id") or "").strip()


async def check_permission(subject: str, operation: str) -> bool:
    payload = {
        "accessor_id": subject,
        "checks": [
            {
                "resource": {
                    "type": AUTHZ_RESOURCE_TYPE,
                    "id": AUTHZ_RESOURCE_ID,
                },
                "operation": operation,
            }
        ],
        "evaluation_scope": "effective",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{BKN_SAFE_URL}/api/safe/v1/authz/checks",
            json=payload,
        )
    if response.status_code != 200:
        raise RuntimeError(f"bkn-safe authorization check failed with HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("allowed"), bool):
        raise RuntimeError("bkn-safe returned an invalid authorization response")
    results = data.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise RuntimeError("bkn-safe returned an invalid authorization result")
    result = results[0]
    if not isinstance(result, dict) or result.get("allowed") != data["allowed"]:
        raise RuntimeError("bkn-safe returned an inconsistent authorization result")
    return data["allowed"]


class PlatformAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate gateway callers and enforce the data-ingestion permission surface."""

    def __init__(self, app):
        super().__init__(app)
        self._allowed_internal_token = INTERNAL_SERVICE_TOKEN

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or uuid4().hex
        request.state.trace_id = trace_id
        path = request.url.path

        if path in PUBLIC_PATHS:
            response = await call_next(request)
            response.headers["x-trace-id"] = trace_id
            response.headers["x-request-id"] = trace_id
            return response

        internal_token = request.headers.get("x-data-ingestion-internal-token", "")
        if self._allowed_internal_token and internal_token and compare_digest(
            internal_token,
            self._allowed_internal_token,
        ):
            request.state.caller = CallerIdentity(
                subject="internal:data-ingestion",
                client_id="data-ingestion",
                account_type="app",
                trace_id=trace_id,
            )
            response = await call_next(request)
            response.headers["x-trace-id"] = trace_id
            response.headers["x-request-id"] = trace_id
            return response

        if not AUTH_ENABLED:
            request.state.caller = CallerIdentity(
                subject="development",
                client_id="data-ingestion",
                account_type="user",
                trace_id=trace_id,
                access_token=request.headers.get("authorization", "").removeprefix(
                    "Bearer "
                ),
            )
            response = await call_next(request)
            response.headers["x-trace-id"] = trace_id
            response.headers["x-request-id"] = trace_id
            return response

        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            return error_response(
                401,
                "UNAUTHORIZED",
                "Authentication is required.",
                trace_id,
            )

        try:
            subject, client_id = await introspect_token(authorization[7:].strip())
        except PermissionError:
            return error_response(
                401,
                "UNAUTHORIZED",
                "The access token is not valid.",
                trace_id,
            )
        except Exception:
            return error_response(
                503,
                "AUTH_SERVICE_UNAVAILABLE",
                "The authentication service is unavailable.",
                trace_id,
            )

        operation = required_operation(request)
        try:
            allowed = await check_permission(subject, operation)
        except Exception:
            return error_response(
                503,
                "AUTHORIZATION_SERVICE_UNAVAILABLE",
                "The authorization service is unavailable.",
                trace_id,
            )
        if not allowed:
            return error_response(
                403,
                "FORBIDDEN",
                "The caller does not have the required data-ingestion permission.",
                trace_id,
            )

        request.state.caller = CallerIdentity(
            subject=subject,
            client_id=client_id,
            account_type="user" if client_id != subject else "app",
            trace_id=trace_id,
            access_token=authorization[7:].strip(),
        )
        response = await call_next(request)
        response.headers["x-trace-id"] = trace_id
        response.headers["x-request-id"] = trace_id
        return response
