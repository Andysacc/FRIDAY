import json
import logging
import re
from uuid import uuid4

from pydantic import ValidationError

from app.agents.coding import CodingAgent
from app.orchestrator.contracts import (
    AgentTaskMessage,
    CodingTaskRequest,
    ExecutionPlan,
    ExecutionRequest,
    OrchestrationResult,
    OrchestratorDecision,
    ProjectBlueprint,
    ProjectCreationRequest,
    ProjectCreationResult,
    ResultSummary,
    utc_now,
)
from app.providers.base import AIProvider, ProviderError
from app.schemas import ChatMessage
from app.state.task_store import TaskStore


logger = logging.getLogger("friday.orchestrator")

PLANNER_PROMPT = """You are Qwen, FRIDAY's permanent task orchestrator.
Return one JSON object only. Delegate this single task to the coding agent.
Choose exactly one allow-listed execution operation:
- python.test: arguments {\"framework\": \"unittest\"|\"pytest\", \"targets\": [relative paths]}
- python.compile: arguments {\"targets\": [relative paths]}
- git.status: arguments {}
- git.diff: arguments {\"staged\": boolean, \"paths\": [relative paths]}
- git.log: arguments {\"limit\": 1..100}
- docker.compose_config or docker.compose_ps: arguments {\"compose_file\": relative path}
- db.sqlite_integrity or db.sqlite_schema: arguments {\"path\": relative SQLite path}
Never emit a shell command, Python source, SQL query, absolute path, or path containing '..'.
Required schema:
{\"schema_version\":\"1.0\",\"agent\":\"coding\",\"execution\":{\"tool\":\"python|git|docker|db\",\"action\":\"...\",\"arguments\":{},\"timeout_seconds\":30}}"""

SUMMARY_PROMPT = """You are Qwen, FRIDAY's permanent task orchestrator.
Merge the coding agent result into one concise JSON object only.
Do not claim success when the agent or execution failed.
Required schema:
{\"status\":\"succeeded|failed\",\"message\":\"plain-language result\",\"key_findings\":[\"...\"]}"""


PROJECT_PLANNER_PROMPT = """You are Qwen, FRIDAY's permanent project orchestrator.
Turn the user's explicit project objective into one practical project blueprint.
Return one JSON object only. Do not include implementation code or filesystem paths.
project_id must be a short lowercase slug using only letters, numbers, and hyphens.
The first milestone must be concrete. Provide 2 to 5 observable success criteria.
Required schema:
{\"project_id\":\"slug\",\"name\":\"display name\",\"description\":\"one-sentence scope\",\"milestone\":\"first milestone\",\"success_criteria\":[\"criterion\"]}"""


class OrchestrationError(RuntimeError):
    pass


class QwenOrchestrator:
    """Owns task state, Qwen planning, delegation, and result merging."""

    def __init__(
        self,
        *,
        provider: AIProvider,
        coding_agent: CodingAgent,
        store: TaskStore,
        default_model: str,
    ) -> None:
        self._provider = provider
        self._coding_agent = coding_agent
        self._store = store
        self._default_model = default_model

    async def run_coding_task(self, request: CodingTaskRequest) -> OrchestrationResult:
        task_id = request.task_id or str(uuid4())
        model = request.model or self._default_model
        created_at = utc_now()
        self._store.create_task(
            task_id=task_id,
            project_id=request.project_id,
            objective=request.objective,
            request=request.model_dump(mode="json"),
        )
        self._log("task.created", task_id=task_id, project_id=request.project_id)

        plan: ExecutionPlan | None = None
        agent_result = None
        try:
            self._store.transition(task_id, "planning")
            plan = request.execution or await self._plan(request.objective, model)
            self._store.transition(task_id, "delegated", payload=plan.model_dump(mode="json"))

            execution = ExecutionRequest(
                task_id=task_id,
                tool=plan.tool,
                action=plan.action,
                arguments=plan.arguments,
                timeout_seconds=plan.timeout_seconds,
            )
            agent_task = AgentTaskMessage(
                task_id=task_id,
                project_id=request.project_id,
                objective=request.objective,
                execution=execution,
            )
            self._store.transition(task_id, "running", payload=agent_task.model_dump(mode="json"))
            self._log(
                "task.delegated",
                task_id=task_id,
                agent="coding",
                tool=plan.tool,
                action=plan.action,
            )
            agent_result = await self._coding_agent.run(agent_task)
            summary = await self._merge(agent_result.model_dump(mode="json"), model)
            status = agent_result.status
            final_payload = {
                "agent_result": agent_result.model_dump(mode="json"),
                "summary": summary.model_dump(mode="json"),
            }
            self._store.transition(
                task_id,
                status,
                payload=final_payload,
                error=agent_result.execution.error if status == "failed" else None,
            )
        except (OrchestrationError, ProviderError, ValidationError, ValueError) as exc:
            status = "failed"
            summary = ResultSummary(
                status="failed",
                message=f"The coding task could not be completed: {exc}",
                key_findings=["No unvalidated operation was executed."],
            )
            self._store.transition(
                task_id,
                "failed",
                payload={"summary": summary.model_dump(mode="json")},
                error=str(exc),
            )
            self._log("task.failed", task_id=task_id, error=str(exc))

        finished_at = utc_now()
        self._log("task.finished", task_id=task_id, status=status)
        return OrchestrationResult(
            task_id=task_id,
            project_id=request.project_id,
            status=status,
            plan=plan,
            agent_result=agent_result,
            summary=summary,
            created_at=created_at,
            finished_at=finished_at,
        )

    async def create_project(self, request: ProjectCreationRequest) -> ProjectCreationResult:
        """Plan and register a project, with separately authorized fixed scaffolding."""

        created_at = utc_now()
        model = request.model or self._default_model
        blueprint = request.blueprint or await self._plan_project(request.objective, model)
        blueprint = blueprint.model_copy(
            update={"project_id": self._available_project_id(blueprint.project_id)}
        )
        project_state = {
            "name": blueprint.name,
            "description": blueprint.description,
            "progress": 0,
            "milestone": blueprint.milestone,
            "status": "on-track",
            "pipeline_stage": 0,
            "success_criteria": blueprint.success_criteria,
        }
        project = self._store.upsert_project(blueprint.project_id, project_state)
        if request.activate:
            project = self._store.set_active_project(blueprint.project_id)
        self._log(
            "project.created",
            project_id=blueprint.project_id,
            source=request.source,
            scaffold=request.scaffold,
        )

        scaffold_result = None
        status = "succeeded"
        if request.scaffold:
            scaffold_result = await self.run_coding_task(
                CodingTaskRequest(
                    project_id=blueprint.project_id,
                    objective=f"Create the approved starter workspace for {blueprint.name}",
                    execution=ExecutionPlan(
                        tool="workspace",
                        action="scaffold",
                        arguments={
                            "project_id": blueprint.project_id,
                            "name": blueprint.name,
                            "description": blueprint.description,
                            "milestone": blueprint.milestone,
                            "objective": request.objective,
                            "success_criteria": blueprint.success_criteria,
                        },
                        timeout_seconds=30,
                    ),
                    model=model,
                )
            )
            if scaffold_result.status != "succeeded":
                status = "partial"

        finished_at = utc_now()
        self._log("project.creation_finished", project_id=blueprint.project_id, status=status)
        return ProjectCreationResult(
            status=status,
            blueprint=blueprint,
            project=project,
            scaffold_result=scaffold_result,
            created_at=created_at,
            finished_at=finished_at,
        )

    async def _plan(self, objective: str, model: str) -> ExecutionPlan:
        raw = await self._provider.chat(
            messages=[
                ChatMessage(role="system", content=PLANNER_PROMPT),
                ChatMessage(role="user", content=json.dumps({"objective": objective})),
            ],
            model=model,
            temperature=0,
        )
        try:
            decision = OrchestratorDecision.model_validate(self._parse_json(raw))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise OrchestrationError(f"Qwen returned an invalid execution plan: {exc}") from exc
        if decision.execution.tool == "workspace":
            raise OrchestrationError("Qwen cannot authorize workspace mutation")
        return decision.execution

    async def _plan_project(self, objective: str, model: str) -> ProjectBlueprint:
        raw = await self._provider.chat(
            messages=[
                ChatMessage(role="system", content=PROJECT_PLANNER_PROMPT),
                ChatMessage(role="user", content=json.dumps({"objective": objective})),
            ],
            model=model,
            temperature=0,
        )
        try:
            return ProjectBlueprint.model_validate(self._parse_json(raw))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise OrchestrationError(f"Qwen returned an invalid project blueprint: {exc}") from exc

    def _available_project_id(self, requested: str) -> str:
        if self._store.get_project(requested) is None:
            return requested
        suffix = 2
        while True:
            suffix_text = f"-{suffix}"
            candidate = f"{requested[:80 - len(suffix_text)]}{suffix_text}"
            if self._store.get_project(candidate) is None:
                return candidate
            suffix += 1

    async def _merge(self, result: dict, model: str) -> ResultSummary:
        fallback_status = "succeeded" if result.get("status") == "succeeded" else "failed"
        try:
            raw = await self._provider.chat(
                messages=[
                    ChatMessage(role="system", content=SUMMARY_PROMPT),
                    ChatMessage(role="user", content=json.dumps(result, sort_keys=True, default=str)),
                ],
                model=model,
                temperature=0,
            )
            summary = ResultSummary.model_validate(self._parse_json(raw))
            if summary.status != fallback_status:
                raise OrchestrationError("Qwen summary status did not match the execution result")
            return summary
        except (ProviderError, json.JSONDecodeError, ValidationError, ValueError, OrchestrationError) as exc:
            execution = result.get("execution", {})
            action = f"{execution.get('tool', 'tool')}.{execution.get('action', 'operation')}"
            error = execution.get("error")
            message = f"{action} {fallback_status}."
            if error:
                message = f"{message} {error}"
            self._log("summary.fallback", error=str(exc))
            return ResultSummary(
                status=fallback_status,
                message=message,
                key_findings=list(result.get("findings") or [])[:10],
            )

    @staticmethod
    def _parse_json(raw: str) -> dict:
        value = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            value = fenced.group(1)
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("response must be a JSON object")
        return parsed

    @staticmethod
    def _log(event: str, **fields) -> None:
        logger.info(json.dumps({"event": event, **fields}, sort_keys=True, default=str))
