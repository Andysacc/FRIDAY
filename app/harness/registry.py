"""Trusted Python plugins; no model-provided imports, code, or package installation."""
import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from app.harness.contracts import StepResult, ToolOutcome, WorkflowPlan


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    project_id: str
    step_id: str
    timeout_seconds: int
    # Only declared dependencies are visible; each consumer receives a copy.
    dependencies: Mapping[str, StepResult]


@dataclass(frozen=True)
class ToolPlugin:
    name: str
    description: str
    arguments_model: type[BaseModel]
    execute: Callable[[BaseModel, ToolContext], Awaitable[ToolOutcome]]
    model_selectable: bool = False
    validate_arguments: Callable[[BaseModel], None] | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolPlugin] = {}

    def register(self, plugin: ToolPlugin) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", plugin.name) or len(plugin.name) > 100:
            raise ValueError("plugin name must have the form namespace.action")
        if plugin.arguments_model.model_config.get("extra") != "forbid":
            raise ValueError("plugin argument models must forbid unknown fields")
        if plugin.name in self._tools:
            raise ValueError(f"tool already registered: {plugin.name}")
        self._tools[plugin.name] = plugin

    def unregister(self, name: str) -> None:
        del self._tools[name]

    def snapshot(self) -> "ToolRegistry":
        registry = ToolRegistry()
        registry._tools = self._tools.copy()
        return registry

    def catalog(self, *, model_only: bool = False) -> list[dict[str, Any]]:
        return [dict(name=tool.name, description=tool.description,
                     model_selectable=tool.model_selectable,
                     arguments_schema=tool.arguments_model.model_json_schema())
                for tool in self._tools.values() if not model_only or tool.model_selectable]

    def validate(self, plan: WorkflowPlan, *, model_planned: bool = False) -> None:
        # Validate the entire graph before the first tool can execute.
        for step in plan.steps:
            plugin = self._tools.get(step.tool)
            if plugin is None:
                raise ValueError(f"unknown tool: {step.tool}")
            if model_planned and not plugin.model_selectable:
                raise ValueError(f"Qwen cannot authorize tool: {step.tool}")
            arguments = plugin.arguments_model.model_validate(step.arguments)
            if plugin.validate_arguments:
                plugin.validate_arguments(arguments)

    async def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
        plugin = self._tools[name]
        validated = plugin.arguments_model.model_validate(arguments)
        if plugin.validate_arguments:
            plugin.validate_arguments(validated)
        try:
            outcome = await asyncio.wait_for(plugin.execute(validated, context), context.timeout_seconds)
            # Snapshot JSON results so plugin-owned mutable objects cannot change recorded facts.
            encoded = outcome.model_dump_json()
            if len(encoded) > 150_000:
                raise ValueError("plugin result exceeds 150000 characters")
            return ToolOutcome.model_validate_json(encoded)
        except TimeoutError:
            return ToolOutcome(status="timed_out", error=f"tool exceeded {context.timeout_seconds}s")


def dependency_snapshot(results: dict[str, StepResult], dependencies: list[str]) -> Mapping[str, StepResult]:
    return MappingProxyType({key: results[key].model_copy(deep=True) for key in dependencies})
