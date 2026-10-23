"""Adapt existing deterministic execution operations to typed harness plugins."""
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StringConstraints, model_validator

from app.execution.service import ExecutionService
from app.harness.contracts import Contract, ToolOutcome
from app.harness.registry import ToolContext, ToolPlugin, ToolRegistry
from app.orchestrator.contracts import ExecutionRequest

SafePath = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class Paths(Contract):
    targets: list[SafePath] = Field(default_factory=lambda: ["app"], min_length=1, max_length=32)


class Tests(Paths):
    targets: list[SafePath] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=32)
    framework: Literal["unittest", "pytest"] = "unittest"
    pattern: str = Field(default="test*.py", min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_discovery(self) -> "Tests":
        if self.framework == "unittest" and len(self.targets) != 1:
            raise ValueError("unittest requires exactly one target")
        if "/" in self.pattern or "\\" in self.pattern:
            raise ValueError("test pattern must be a filename pattern")
        return self


class Diff(Contract):
    staged: StrictBool = False
    paths: list[SafePath] = Field(default_factory=list, max_length=32)


class Log(Contract):
    limit: int = Field(default=10, ge=1, le=100, strict=True)


class Compose(Contract):
    compose_file: SafePath = "docker-compose.yml"


class Database(Contract):
    path: SafePath = "data/friday.db"


def build_registry(executor: ExecutionService) -> ToolRegistry:
    registry = ToolRegistry()
    operations = [
        ("python.test", Tests, "Run Python unit tests in the configured workspace."),
        ("python.compile", Paths, "Compile Python sources to check syntax."),
        ("git.status", Contract, "Inspect Git working tree status."),
        ("git.diff", Diff, "Inspect Git changes."),
        ("git.log", Log, "Inspect recent Git commits."),
        ("docker.compose_config", Compose, "Validate Docker Compose configuration."),
        ("docker.compose_ps", Compose, "Inspect Docker Compose services."),
        ("db.sqlite_integrity", Database, "Check an existing SQLite database in read-only mode."),
        ("db.sqlite_schema", Database, "Inspect an existing SQLite schema in read-only mode."),
    ]

    def validate_paths(arguments: Contract) -> None:
        values = arguments.model_dump()
        paths = values.get("targets", values.get("paths", []))
        paths += [values[key] for key in ("compose_file", "path") if key in values]
        for value in paths:
            path = Path(value)
            if (path.is_absolute() or ".." in path.parts or value != value.strip()
                    or any(part.startswith("-") for part in path.parts) or "\\" in value):
                raise ValueError("tool paths must be workspace-relative, without traversal or option prefixes")
            if not (executor.workspace / path).resolve().is_relative_to(executor.workspace):
                raise ValueError("tool path escapes the workspace")

    for name, arguments_model, description in operations:
        tool, action = name.split(".")

        async def execute(arguments, context: ToolContext, tool=tool, action=action):
            result = await executor.execute(ExecutionRequest(
                task_id=context.task_id, tool=tool, action=action,
                arguments=arguments.model_dump(), timeout_seconds=context.timeout_seconds,
            ))
            return ToolOutcome(status=result.status, error=result.error, execution=result)

        registry.register(ToolPlugin(name, description, arguments_model, execute,
                                     model_selectable=True, validate_arguments=validate_paths))
    return registry
