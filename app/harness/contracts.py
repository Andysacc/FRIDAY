"""Versioned contracts for FRIDAY's plugin-based multi-tool harness."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.orchestrator.contracts import ExecutionResult, ResultSummary


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowStep(Contract):
    step_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    tool: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class WorkflowPlan(Contract):
    steps: list[WorkflowStep] = Field(min_length=1, max_length=32)
    max_parallel: int = Field(default=2, ge=1, le=4)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowPlan":
        ids = {step.step_id for step in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("step IDs must be unique")
        remaining = {}
        for step in self.steps:
            deps = set(step.depends_on)
            if len(deps) != len(step.depends_on):
                raise ValueError("dependencies must be unique")
            if step.step_id in deps or not deps <= ids:
                raise ValueError("dependencies must name other steps in this workflow")
            remaining[step.step_id] = deps
        resolved: set[str] = set()
        while remaining:
            ready = {key for key, deps in remaining.items() if deps <= resolved}
            if not ready:
                raise ValueError("workflow contains a dependency cycle")
            resolved.update(ready)
            remaining = {key: deps for key, deps in remaining.items() if key not in ready}
        return self


class WorkflowDefinition(Contract):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    plan: WorkflowPlan


class WorkflowRequest(Contract):
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    project_id: str = Field(default="friday", pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    objective: str = Field(default="Run the selected workflow", min_length=3, max_length=4000)
    workflow_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    plan: WorkflowPlan | None = None
    model: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "WorkflowRequest":
        if self.workflow_id and self.plan:
            raise ValueError("choose a saved workflow or an inline plan")
        return self


class ToolOutcome(Contract):
    status: Literal["succeeded", "failed", "timed_out", "rejected"]
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=2000)
    execution: ExecutionResult | None = None


class StepResult(Contract):
    step_id: str
    tool: str
    status: Literal["succeeded", "failed", "timed_out", "rejected", "skipped"]
    outcome: ToolOutcome | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime


class WorkflowResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    project_id: str
    status: Literal["succeeded", "failed"]
    plan: WorkflowPlan | None
    steps: list[StepResult]
    summary: ResultSummary
    created_at: datetime
    finished_at: datetime
