#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import mcp_server


class McpPipelineTest(unittest.TestCase):
    def test_run_user_pipeline_resolves_inputs_and_previous_outputs(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def source_tool(query: str = "") -> dict[str, object]:
            calls.append(("source_tool", {"query": query}))
            return {"id": "dataset-1", "query": query}

        def consumer_tool(dataset_id: str, limit: int = 10) -> dict[str, object]:
            calls.append(("consumer_tool", {"dataset_id": dataset_id, "limit": limit}))
            return {"rows": 2, "dataset_id": dataset_id}

        def request(method: str, path: str, **_kwargs: object):
            if method == "GET" and path == "/api/templates/user-test":
                return {
                    "code": "user-test",
                    "name": "Test pipeline",
                    "steps": [
                        {
                            "key": "step-1",
                            "name": "Source",
                            "tool_name": "source_tool",
                            "parameters": {"query": "${inputs.query}"},
                        },
                        {
                            "key": "step-2",
                            "name": "Consumer",
                            "tool_name": "consumer_tool",
                            "parameters": {
                                "dataset_id": "${steps.step-1.result.id}",
                                "limit": 10,
                            },
                        },
                    ],
                }
            if method == "POST" and path == "/api/capabilities/pipeline-executions":
                return {"id": "execution-1"}
            if method == "PATCH" and path == "/api/capabilities/pipeline-executions/execution-1":
                return {"id": "execution-1"}
            self.fail(f"Unexpected request: {method} {path}")

        with (
            patch.object(mcp_server, "_request", side_effect=request),
            patch.dict(
                mcp_server.__dict__,
                {"source_tool": source_tool, "consumer_tool": consumer_tool},
            ),
        ):
            result = mcp_server.run_user_pipeline(
                "user-test",
                inputs={"query": "marine"},
                step_inputs={"step-2": {"limit": 25}},
            )

        self.assertEqual(
            calls,
            [
                ("source_tool", {"query": "marine"}),
                ("consumer_tool", {"dataset_id": "dataset-1", "limit": 25}),
            ],
        )
        self.assertEqual(result["results"][1]["result"], {"rows": 2, "dataset_id": "dataset-1"})
        self.assertEqual(result["execution_id"], "execution-1")


if __name__ == "__main__":
    unittest.main()
