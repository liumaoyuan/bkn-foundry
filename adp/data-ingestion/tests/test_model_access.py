# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import inspect
import unittest
from collections.abc import Coroutine
from typing import Any, TypeVar
from unittest.mock import patch

from app.model_access import default_model_status, post_chat_completion


T = TypeVar("T")


def run_immediately(coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive the mocked coroutine tree without relying on the host event loop."""
    value: Any = None
    while True:
        try:
            awaited = coroutine.send(value)
        except StopIteration as stopped:
            return stopped.value
        if not inspect.iscoroutine(awaited):
            raise AssertionError(f"unexpected awaitable: {type(awaited)!r}")
        value = run_immediately(awaited)


class FakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> dict[str, object]:
        return self._payload


class FakeAsyncClient:
    def __init__(
        self,
        *,
        get_response: FakeResponse | None = None,
        post_response: FakeResponse | None = None,
    ) -> None:
        self.get_response = get_response
        self.post_response = post_response
        self.get_call: dict[str, object] = {}
        self.post_call: dict[str, object] = {}

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.get_call = {"url": url, **kwargs}
        assert self.get_response is not None
        return self.get_response

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.post_call = {"url": url, **kwargs}
        assert self.post_response is not None
        return self.post_response


class ModelAccessTest(unittest.TestCase):
    def test_status_returns_platform_default_model(self) -> None:
        client = FakeAsyncClient(
            get_response=FakeResponse(
                {
                    "count": 2,
                    "data": [
                        {
                            "model_id": "model-1",
                            "model_name": "not-default",
                            "f_default": 0,
                        },
                        {
                            "model_id": "model-2",
                            "model_name": "platform-default",
                            "f_default": 1,
                        },
                    ],
                }
            )
        )

        with patch(
            "app.model_access.httpx.AsyncClient",
            side_effect=lambda **_kwargs: client,
        ):
            status = run_immediately(default_model_status("access-token"))

        self.assertTrue(status["configured"])
        self.assertEqual(status["managed_by"], "model-resources")
        self.assertEqual(status["default_model_id"], "model-2")
        self.assertEqual(status["default_model_name"], "platform-default")
        self.assertEqual(
            client.get_call["headers"],
            {
                "Authorization": "Bearer access-token",
                "Accept-Language": "zh-CN",
                "Content-Type": "application/json",
            },
        )

    def test_status_reports_no_default_without_guessing(self) -> None:
        client = FakeAsyncClient(
            get_response=FakeResponse(
                {
                    "data": [
                        {
                            "model_id": "model-1",
                            "model_name": "available-but-not-default",
                            "f_default": 0,
                        }
                    ]
                }
            )
        )

        with patch(
            "app.model_access.httpx.AsyncClient",
            side_effect=lambda **_kwargs: client,
        ):
            status = run_immediately(default_model_status("access-token"))

        self.assertFalse(status["configured"])
        self.assertEqual(status["default_model_name"], "")

    def test_status_omits_authorization_header_without_a_caller_token(self) -> None:
        client = FakeAsyncClient(
            get_response=FakeResponse({"data": []})
        )

        with patch(
            "app.model_access.httpx.AsyncClient",
            side_effect=lambda **_kwargs: client,
        ):
            run_immediately(default_model_status(""))

        self.assertNotIn("Authorization", client.get_call["headers"])

    def test_chat_completion_uses_platform_model_and_caller_token(self) -> None:
        client = FakeAsyncClient(post_response=FakeResponse({"choices": []}))
        payload = {
            "model": "",
            "model_id": "",
            "messages": [{"role": "user", "content": "hello"}],
        }

        with patch(
            "app.model_access.httpx.AsyncClient",
            side_effect=lambda **_kwargs: client,
        ):
            response = run_immediately(
                post_chat_completion("caller-token", payload)
            )

        self.assertEqual(response, {"choices": []})
        self.assertEqual(client.post_call["json"], payload)
        headers = client.post_call["headers"]
        self.assertEqual(headers["Authorization"], "Bearer caller-token")
        self.assertTrue(str(client.post_call["url"]).endswith("/chat/completions"))


if __name__ == "__main__":
    unittest.main()
