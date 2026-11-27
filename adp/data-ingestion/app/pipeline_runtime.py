#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any


REFERENCE_PATTERN = re.compile(r"\$\{([^{}]+)\}")


class PipelineRuntimeError(ValueError):
    """Raised when a user pipeline cannot resolve its parameters."""


def _reference_value(reference: str, context: Mapping[str, Any]) -> Any:
    tokens = [token for token in reference.strip().split(".") if token]
    if not tokens:
        raise PipelineRuntimeError("管线参数引用不能为空")
    value: Any = context
    for token in tokens:
        if isinstance(value, Mapping):
            if token not in value:
                raise PipelineRuntimeError(f"管线参数引用不存在：{reference}")
            value = value[token]
            continue
        if isinstance(value, list):
            try:
                value = value[int(token)]
            except (ValueError, IndexError) as error:
                raise PipelineRuntimeError(f"管线参数引用不存在：{reference}") from error
            continue
        raise PipelineRuntimeError(f"管线参数引用无法继续解析：{reference}")
    return value


def resolve_pipeline_value(value: Any, context: Mapping[str, Any]) -> Any:
    """Resolve exact and embedded `${inputs.foo}` / `${steps.step-1.result.bar}` refs."""
    if isinstance(value, dict):
        return {key: resolve_pipeline_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_pipeline_value(item, context) for item in value]
    if not isinstance(value, str):
        return value

    matches = list(REFERENCE_PATTERN.finditer(value))
    if not matches:
        return value
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return _reference_value(matches[0].group(1), context)

    def replace(match: re.Match[str]) -> str:
        resolved = _reference_value(match.group(1), context)
        return str(resolved)

    return REFERENCE_PATTERN.sub(replace, value)


def execute_pipeline_steps(
    steps: list[dict[str, Any]],
    *,
    inputs: Mapping[str, Any] | None = None,
    step_inputs: Mapping[str, Mapping[str, Any]] | None = None,
    call_tool: Callable[[str, dict[str, Any]], Any],
    on_step_start: Callable[[dict[str, Any], dict[str, Any], int], None] | None = None,
    on_step_result: Callable[[dict[str, Any], int], None] | None = None,
) -> list[dict[str, Any]]:
    """Execute steps sequentially while exposing earlier results to later steps."""
    context: dict[str, Any] = {
        "inputs": dict(inputs or {}),
        "steps": {},
    }
    run_inputs = step_inputs or {}
    results: list[dict[str, Any]] = []

    for index, step in enumerate(steps, start=1):
        tool_name = str(step.get("tool_name") or "").strip()
        if not tool_name:
            raise PipelineRuntimeError(f"管线步骤 {index} 未选择 MCP 工具")
        step_key = str(step.get("key") or f"tool-{index}")
        if on_step_start:
            on_step_start(step, {}, index)
        configured = resolve_pipeline_value(step.get("parameters") or {}, context)
        provided = resolve_pipeline_value(
            run_inputs.get(str(index), run_inputs.get(step_key, {})),
            context,
        )
        if not isinstance(configured, dict):
            raise PipelineRuntimeError(f"管线步骤 {index} 的固定参数必须是对象")
        if not isinstance(provided, dict):
            raise PipelineRuntimeError(f"管线步骤 {index} 的运行参数必须是对象")
        arguments = {**configured, **provided}
        result = call_tool(tool_name, arguments)
        step_result = {
            "step": index,
            "key": step_key,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
        }
        results.append(step_result)
        if on_step_result:
            on_step_result(step_result, index)
        context["steps"][str(index)] = step_result
        context["steps"][step_key] = step_result

    return results
