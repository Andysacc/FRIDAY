from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


SCHEMA_VERSION = "1.0"

TaskStatus = Literal[
    "queued",
    "planning",
    "delegated",
    "running",
    "succeeded",
    "failed",
]
ExecutionTool = Literal["python", "git", "docker", "db", "workspace"]
ExecutionStatus = Literal["succeeded", "failed", "timed_out", "rejected"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionPlan(BaseModel):
    """A validated operation selected by Qwen, without process-level details."""

    tool: ExecutionTool
    action: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=1, le=120)

    @model_validator(mode="after")
    def validate_tool_action(self) -> "ExecutionPlan":
        allowed = {
            "python": {"test", "compile"},
            "git": {"status", "diff", "log"},
            "docker": {"compose_config", "compose_ps"},
            "db": {"sqlite_integrity", "sqlite_schema"},
            "workspace": {"scaffold"},
        }
        if self.action not in allowed[self.tool]:
            choices = ", ".join(sorted(allowed[self.tool]))
            raise ValueError(f"{self.tool} action must be one of: {choices}")
        return self


class ExecutionRequest(BaseModel):
    """Structured message sent to the deterministic execution boundary."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    tool: ExecutionTool
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class ExecutionResult(BaseModel):
    """Structured message returned by the deterministic execution boundary."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    execution_id: str
    task_id: str
    tool: ExecutionTool
    action: str
    status: ExecutionStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)


class AgentTaskMessage(BaseModel):
    """Structured task passed from the orchestrator to a specialist agent."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    project_id: str
    agent: Literal["coding"] = "coding"
    objective: str
    execution: ExecutionRequest
    created_at: datetime = Field(default_factory=utc_now)


class AgentResultMessage(BaseModel):
    """Structured result passed from a specialist agent to the orchestrator."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    agent: Literal["coding"] = "coding"
    status: Literal["succeeded", "failed"]
    execution: ExecutionResult
    findings: list[str] = Field(default_factory=list)


class OrchestratorDecision(BaseModel):
    """Validated coding task plan accepted from Qwen."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    agent: Literal["coding"]
    execution: ExecutionPlan


class ResultSummary(BaseModel):
    status: Literal["succeeded", "failed"]
    message: str = Field(min_length=1, max_length=1000)
    key_findings: list[str] = Field(default_factory=list, max_length=10)


class CodingTaskRequest(BaseModel):
    """Public request for the first supported orchestration path."""

    task_id: str | None = None
    project_id: str = Field(default="friday", pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    objective: str = Field(min_length=3, max_length=4000)
    execution: ExecutionPlan | None = None
    model: str | None = None


class ProjectBlueprint(BaseModel):
    """Validated project definition proposed by Qwen before any state is changed."""

    project_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)
    milestone: str = Field(min_length=1, max_length=80)
    success_criteria: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_success_criteria(self) -> "ProjectBlueprint":
        cleaned = [criterion.strip() for criterion in self.success_criteria]
        if any(not criterion or len(criterion) > 160 for criterion in cleaned):
            raise ValueError("success criteria must contain 1 to 160 characters")
        self.success_criteria = cleaned
        return self


class ProjectCreationRequest(BaseModel):
    """Explicit authorization to turn an objective into a durable project."""

    objective: str = Field(min_length=3, max_length=4000)
    source: Literal["dashboard", "chat", "voice", "api"] = "api"
    activate: bool = True
    scaffold: bool = False
    blueprint: ProjectBlueprint | None = None
    model: str | None = None


class OrchestrationResult(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: str
    project_id: str
    status: Literal["succeeded", "failed"]
    agent: Literal["coding"] = "coding"
    plan: ExecutionPlan | None = None
    agent_result: AgentResultMessage | None = None
    summary: ResultSummary
    created_at: datetime
    finished_at: datetime


class ProjectCreationResult(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    status: Literal["succeeded", "partial"]
    blueprint: ProjectBlueprint
    project: dict[str, Any]
    scaffold_result: OrchestrationResult | None = None
    created_at: datetime
    finished_at: datetime
