# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from app.agent_api import current_access_token
from app.auth import error_response, required_operation
from app.auth import CallerIdentity


def request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )


class DataIngestionAuthTest(unittest.TestCase):
    def test_required_operation_uses_platform_permission_surface(self) -> None:
        self.assertEqual(required_operation(request("GET", "/api/datasets")), "view")
        self.assertEqual(required_operation(request("POST", "/api/runs")), "create")
        self.assertEqual(
            required_operation(request("POST", "/api/agent/sessions")),
            "agent_use",
        )
        self.assertEqual(
            required_operation(request("POST", "/api/auto-scan/jobs")),
            "task_manage",
        )
        self.assertEqual(
            required_operation(request("POST", "/api/spaces")),
            "task_manage",
        )

    def test_error_response_has_unified_contract_and_trace_headers(self) -> None:
        response = error_response(403, "FORBIDDEN", "denied", "trace-1")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.body.decode(),
            '{"error_code":"FORBIDDEN","message":"denied","trace_id":"trace-1"}',
        )
        self.assertEqual(response.headers["x-trace-id"], "trace-1")
        self.assertEqual(response.headers["x-request-id"], "trace-1")

    def test_agent_requires_an_access_token_when_auth_is_enabled(self) -> None:
        incoming = request("POST", "/api/agent/sessions")
        incoming.state.caller = CallerIdentity(
            subject="alice",
            client_id="studio",
            account_type="user",
            trace_id="trace-1",
        )

        with patch("app.agent_api.AUTH_ENABLED", True):
            with self.assertRaises(HTTPException) as raised:
                current_access_token(incoming)

        self.assertEqual(raised.exception.status_code, 401)

