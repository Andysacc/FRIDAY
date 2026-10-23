import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
from uuid import uuid4
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.orchestrator.contracts import ExecutionRequest, ExecutionResult, utc_now


MAX_OUTPUT_CHARS = 50_000


class ExecutionRejected(ValueError):
    pass


class ExecutionService:
    """Deterministic, allow-listed process and database execution boundary."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._handlers: dict[str, Callable[[ExecutionRequest], Awaitable[ExecutionResult]]] = {
            "python": self._execute_python,
            "git": self._execute_git,
            "docker": self._execute_docker,
            "db": self._execute_db,
            "workspace": self._execute_workspace,
        }

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started_at = utc_now()
        started = time.monotonic()
        try:
            if not self.workspace.is_dir():
                raise ExecutionRejected(f"workspace does not exist: {self.workspace}")
            return await self._handlers[request.tool](request)
        except ExecutionRejected as exc:
            return self._result(
                request,
                status="rejected",
                started_at=started_at,
                started=started,
                error=str(exc),
            )
        except Exception as exc:
            return self._result(
                request,
                status="failed",
                started_at=started_at,
                started=started,
                error=f"execution service error: {exc}",
            )

    async def _execute_python(self, request: ExecutionRequest) -> ExecutionResult:
        arguments = request.arguments
        if request.action == "test":
            self._require_keys(arguments, {"framework", "targets", "pattern"})
            framework = self._string(arguments.get("framework", "unittest"), "framework")
            targets = self._path_list(arguments.get("targets", ["tests"]), "targets")
            if framework == "unittest":
                if len(targets) != 1:
                    raise ExecutionRejected("unittest requires exactly one target directory")
                pattern = self._string(arguments.get("pattern", "test*.py"), "pattern")
                if "/" in pattern or "\\" in pattern:
                    raise ExecutionRejected("unittest pattern must be a file pattern, not a path")
                argv = [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    targets[0],
                    "-p",
                    pattern,
                ]
            elif framework == "pytest":
                argv = [sys.executable, "-m", "pytest", *targets]
            else:
                raise ExecutionRejected("framework must be 'unittest' or 'pytest'")
        elif request.action == "compile":
            self._require_keys(arguments, {"targets"})
            targets = self._path_list(arguments.get("targets", ["app"]), "targets")
            argv = [sys.executable, "-m", "compileall", "-q", *targets]
        else:
            raise ExecutionRejected("python action must be 'test' or 'compile'")

        return await self._run_command(request, argv)

    async def _execute_git(self, request: ExecutionRequest) -> ExecutionResult:
        arguments = request.arguments
        if request.action == "status":
            self._require_keys(arguments, set())
            argv = ["git", "status", "--short", "--branch"]
        elif request.action == "diff":
            self._require_keys(arguments, {"staged", "paths"})
            staged = self._boolean(arguments.get("staged", False), "staged")
            paths = self._path_list(arguments.get("paths", []), "paths", allow_empty=True)
            argv = ["git", "diff", "--no-ext-diff"]
            if staged:
                argv.append("--cached")
            if paths:
                argv.extend(["--", *paths])
        elif request.action == "log":
            self._require_keys(arguments, {"limit"})
            limit = self._integer(arguments.get("limit", 10), "limit", minimum=1, maximum=100)
            argv = ["git", "log", "--oneline", "--no-decorate", "-n", str(limit)]
        else:
            raise ExecutionRejected("git action must be 'status', 'diff', or 'log'")

        return await self._run_command(request, argv)

    async def _execute_docker(self, request: ExecutionRequest) -> ExecutionResult:
        arguments = request.arguments
        self._require_keys(arguments, {"compose_file"})
        compose_file = self._relative_path(
            self._string(arguments.get("compose_file", "docker-compose.yml"), "compose_file")
        )
        if request.action == "compose_config":
            argv = ["docker", "compose", "-f", compose_file, "config"]
        elif request.action == "compose_ps":
            argv = ["docker", "compose", "-f", compose_file, "ps", "--format", "json"]
        else:
            raise ExecutionRejected("docker action must be 'compose_config' or 'compose_ps'")

        return await self._run_command(request, argv)

    async def _execute_db(self, request: ExecutionRequest) -> ExecutionResult:
        arguments = request.arguments
        self._require_keys(arguments, {"path"})
        relative_path = self._relative_path(self._string(arguments.get("path", "data/friday.db"), "path"))
        database_path = self.workspace / relative_path
        if not database_path.is_file():
            raise ExecutionRejected(f"database does not exist: {relative_path}")

        if request.action not in {"sqlite_integrity", "sqlite_schema"}:
            raise ExecutionRejected("db action must be 'sqlite_integrity' or 'sqlite_schema'")

        started_at = utc_now()
        started = time.monotonic()
        try:
            output = await asyncio.wait_for(
                asyncio.to_thread(self._read_sqlite, database_path, request.action),
                timeout=request.timeout_seconds,
            )
            return self._result(
                request,
                status="succeeded",
                started_at=started_at,
                started=started,
                exit_code=0,
                stdout=json.dumps(output, sort_keys=True),
                metadata={"database": relative_path, "read_only": True},
            )
        except TimeoutError:
            return self._result(
                request,
                status="timed_out",
                started_at=started_at,
                started=started,
                error=f"database operation exceeded {request.timeout_seconds}s",
            )
        except sqlite3.Error as exc:
            return self._result(
                request,
                status="failed",
                started_at=started_at,
                started=started,
                error=f"sqlite error: {exc}",
            )

    async def _execute_workspace(self, request: ExecutionRequest) -> ExecutionResult:
        if request.action != "scaffold":
            raise ExecutionRejected("workspace action must be 'scaffold'")

        arguments = request.arguments
        self._require_keys(
            arguments,
            {"project_id", "name", "description", "milestone", "objective", "success_criteria"},
        )
        project_id = self._string(arguments.get("project_id"), "project_id")
        if not project_id.replace("-", "").replace("_", "").replace(".", "").isalnum() or len(project_id) > 80:
            raise ExecutionRejected("project_id must contain only letters, numbers, '.', '-', or '_'")
        name = self._bounded_string(arguments.get("name"), "name", maximum=80)
        description = self._bounded_string(arguments.get("description"), "description", maximum=240)
        milestone = self._bounded_string(arguments.get("milestone"), "milestone", maximum=80)
        objective = self._bounded_string(arguments.get("objective"), "objective", maximum=4000)
        criteria = self._string_list(
            arguments.get("success_criteria"),
            "success_criteria",
            minimum=1,
            maximum=5,
            item_maximum=160,
        )
        relative_directory = self._relative_path(f"projects/{project_id}")
        target = self.workspace / relative_directory
        if target.exists() or target.is_symlink():
            raise ExecutionRejected(f"project directory already exists: {relative_directory}")

        started_at = utc_now()
        started = time.monotonic()
        created_files = await asyncio.to_thread(
            self._write_project_scaffold,
            target,
            project_id=project_id,
            name=name,
            description=description,
            milestone=milestone,
            objective=objective,
            success_criteria=criteria,
        )
        return self._result(
            request,
            status="succeeded",
            started_at=started_at,
            started=started,
            exit_code=0,
            stdout="\n".join(created_files),
            metadata={"directory": relative_directory, "created_files": created_files},
        )

    def _write_project_scaffold(
        self,
        target: Path,
        *,
        project_id: str,
        name: str,
        description: str,
        milestone: str,
        objective: str,
        success_criteria: list[str],
    ) -> list[str]:
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary = parent / f".friday-{project_id}-{uuid4().hex}.tmp"
        temporary.mkdir()
        try:
            criteria_markdown = "\n".join(f"- [ ] {criterion}" for criterion in success_criteria)
            readme = (
                f"# {name}\n\n{description}\n\n"
                f"## Objective\n\n{objective}\n\n"
                f"## First milestone\n\n{milestone}\n\n"
                f"## Success criteria\n\n{criteria_markdown}\n"
            )
            metadata = {
                "schema_version": "1.0",
                "project_id": project_id,
                "name": name,
                "description": description,
                "objective": objective,
                "milestone": milestone,
                "success_criteria": success_criteria,
            }
            (temporary / "README.md").write_text(readme, encoding="utf-8")
            (temporary / ".friday-project.json").write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.rename(target)
        except Exception:
            for child in temporary.iterdir() if temporary.exists() else []:
                child.unlink()
            if temporary.exists():
                temporary.rmdir()
            raise
        return [f"projects/{project_id}/README.md", f"projects/{project_id}/.friday-project.json"]

    async def _run_command(self, request: ExecutionRequest, argv: list[str]) -> ExecutionResult:
        started_at = utc_now()
        started = time.monotonic()
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.workspace,
                env=env,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return self._result(
                request,
                status="failed",
                started_at=started_at,
                started=started,
                error=f"executable unavailable: {exc.filename}",
                metadata={"argv": argv},
            )

        stdout_task = asyncio.create_task(self._read_limited(process.stdout))
        stderr_task = asyncio.create_task(self._read_limited(process.stderr))
        try:
            await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
        except asyncio.CancelledError:
            # A cancelled workflow must not leave its subprocess tree running.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            return self._result(
                request,
                status="timed_out",
                started_at=started_at,
                started=started,
                stdout=stdout,
                stderr=stderr,
                error=f"command exceeded {request.timeout_seconds}s",
                metadata={"argv": argv},
            )

        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return self._result(
            request,
            status="succeeded" if process.returncode == 0 else "failed",
            started_at=started_at,
            started=started,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            error=None if process.returncode == 0 else "command returned a non-zero exit code",
            metadata={"argv": argv},
        )

    def _read_sqlite(self, database_path: Path, action: str) -> dict[str, Any]:
        uri = f"{database_path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            if action == "sqlite_integrity":
                rows = connection.execute("PRAGMA integrity_check").fetchall()
                return {"integrity_check": [row[0] for row in rows]}
            rows = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index', 'view', 'trigger') ORDER BY type, name"
            ).fetchall()
            return {
                "objects": [
                    {"type": row[0], "name": row[1], "table": row[2], "sql": row[3]}
                    for row in rows
                ]
            }

    def _relative_path(self, value: str) -> str:
        candidate = Path(value)
        if candidate.is_absolute():
            raise ExecutionRejected("paths must be relative to the configured workspace")
        resolved = (self.workspace / candidate).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ExecutionRejected("path escapes the configured workspace")
        return str(resolved.relative_to(self.workspace)) or "."

    def _path_list(self, value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
        if not isinstance(value, list) or (not value and not allow_empty):
            raise ExecutionRejected(f"{field} must be a{' non-empty' if not allow_empty else ''} list")
        return [self._relative_path(self._string(item, field)) for item in value]

    def _string_list(
        self,
        value: Any,
        field: str,
        *,
        minimum: int,
        maximum: int,
        item_maximum: int,
    ) -> list[str]:
        if not isinstance(value, list) or not minimum <= len(value) <= maximum:
            raise ExecutionRejected(f"{field} must contain {minimum} to {maximum} items")
        return [self._bounded_string(item, field, maximum=item_maximum) for item in value]

    @staticmethod
    def _require_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
        unknown = set(arguments) - allowed
        if unknown:
            raise ExecutionRejected(f"unsupported arguments: {', '.join(sorted(unknown))}")

    @staticmethod
    def _string(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ExecutionRejected(f"{field} must be a non-empty string")
        return value.strip()

    @classmethod
    def _bounded_string(cls, value: Any, field: str, *, maximum: int) -> str:
        cleaned = cls._string(value, field)
        if len(cleaned) > maximum:
            raise ExecutionRejected(f"{field} must be at most {maximum} characters")
        return cleaned

    @staticmethod
    def _boolean(value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise ExecutionRejected(f"{field} must be a boolean")
        return value

    @staticmethod
    def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ExecutionRejected(f"{field} must be an integer from {minimum} to {maximum}")
        return value

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader | None) -> str:
        if stream is None:
            return ""
        chunks: list[bytes] = []
        captured = 0
        truncated = False
        while chunk := await stream.read(8192):
            remaining = MAX_OUTPUT_CHARS - captured
            if remaining > 0:
                kept = chunk[:remaining]
                chunks.append(kept)
                captured += len(kept)
            if len(chunk) > remaining:
                truncated = True
        value = b"".join(chunks).decode(errors="replace")
        return value + ("\n[output truncated]" if truncated else "")

    @staticmethod
    def _result(
        request: ExecutionRequest,
        *,
        status: str,
        started_at,
        started: float,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        finished_at = utc_now()
        return ExecutionResult(
            execution_id=request.execution_id,
            task_id=request.task_id,
            tool=request.tool,
            action=request.action,
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            error=error,
            metadata=metadata or {},
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )
