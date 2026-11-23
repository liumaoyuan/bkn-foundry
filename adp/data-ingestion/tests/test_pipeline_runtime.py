#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest

from app.pipeline_runtime import (
    PipelineRuntimeError,
    execute_pipeline_steps,
    resolve_pipeline_value,
)


class PipelineRuntimeTest(unittest.TestCase):
    def test_resolves_nested_and_embedded_references(self) -> None:
        context = {
            "inputs": {"file_path": "/data/input.csv", "limit": 10},
            "steps": {"step-1": {"result": {"dataset": {"id": "dataset-1"}}}},
        }
        self.assertEqual(
            resolve_pipeline_value("${steps.step-1.result.dataset}", context),
            {"id": "dataset-1"},
        )
        self.assertEqual(
            resolve_pipeline_value("dataset=${steps.step-1.result.dataset.id}", context),
            "dataset=dataset-1",
        )
        self.assertEqual(
            resolve_pipeline_value({"file": "${inputs.file_path}"}, context),
            {"file": "/data/input.csv"},
        )

    def test_missing_reference_reports_tool_parameter_error(self) -> None:
        with self.assertRaises(PipelineRuntimeError):
            resolve_pipeline_value("${inputs.missing}", {"inputs": {}})

    def test_steps_pass_results_and_merge_run_overrides(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def call_tool(tool_name: str, arguments: dict[str, object]) -> object:
            calls.append((tool_name, arguments))
            if tool_name == "list_datasets":
                return [{"id": "dataset-1", "name": "Imported"}]
            return {"dataset_id": arguments["dataset_id"], "done": True}

        results = execute_pipeline_steps(
            [
                {
                    "key": "step-1",
                    "tool_name": "list_datasets",
                    "parameters": {"query": "${inputs.query}"},
                },
                {
                    "key": "step-2",
                    "tool_name": "get_dataset_schema",
                    "parameters": {
                        "dataset_id": "${steps.step-1.result.0.id}",
                        "sample_size": 20,
                    },
                },
            ],
            inputs={"query": "Imported"},
            step_inputs={"2": {"sample_size": 50}},
            call_tool=call_tool,
        )

        self.assertEqual(calls[0], ("list_datasets", {"query": "Imported"}))
        self.assertEqual(
            calls[1],
            ("get_dataset_schema", {"dataset_id": "dataset-1", "sample_size": 50}),
        )
        self.assertEqual(results[1]["step"], 2)


if __name__ == "__main__":
    unittest.main()
