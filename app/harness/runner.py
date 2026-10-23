"""Qwen plans; the harness validates, schedules, and records authoritative results."""
import asyncio
import json
from uuid import uuid4

from app.harness.contracts import (
    StepResult, WorkflowDefinition, WorkflowPlan, WorkflowRequest, WorkflowResult,
)
from app.harness.registry import ToolContext, ToolRegistry, dependency_snapshot
from app.orchestrator.contracts import ResultSummary, utc_now
from app.orchestrator.qwen import QwenOrchestrator
from app.providers.base import AIProvider
from app.schemas import ChatMessage
from app.state.task_store import TaskStore


PLANNER_PROMPT = """You are Qwen, FRIDAY's permanent coordinator.
Compose a bounded workflow using only the supplied tool catalog and its argument schemas.
Return one JSON object, no commentary or code. At most 8 steps. Depend only on step IDs
in this plan. Independent checks may run in parallel. Tool results cannot be interpolated
into arguments. Never emit shell text, source code, imports, paths outside the workspace,
or tools absent from the catalog. Schema:
{"steps":[{"step_id":"check","tool":"git.status","arguments":{},"depends_on":[],"timeout_seconds":30}],"max_parallel":2}
"""


class HarnessRunner:
    def __init__(self, *, registry: ToolRegistry, store: TaskStore,
                 provider: AIProvider, default_model: str) -> None:
        self.registry = registry
        self.store = store
        self.provider = provider
        self.default_model = default_model

    async def run(self, request: WorkflowRequest) -> WorkflowResult:
        task_id = request.task_id or str(uuid4())
        created_at = utc_now()
        registry = self.registry.snapshot()
        self.store.create_task(task_id=task_id, project_id=request.project_id,
                               objective=request.objective, request=request.model_dump(mode="json"),
                               task_type="workflow")
        plan = None
        results: list[StepResult] = []
        try:
            self.store.transition(task_id, "planning")
            plan = request.plan
            if request.workflow_id:
                saved = self.store.get_workflow(request.workflow_id)
                if saved is None:
                    raise ValueError(f"unknown workflow: {request.workflow_id}")
                plan = WorkflowDefinition.model_validate(saved).plan
            model_planned = plan is None
            if model_planned:
                raw = await self.provider.chat(
                    messages=[ChatMessage(role="system", content=PLANNER_PROMPT),
                              ChatMessage(role="user", content=json.dumps({
                                  "objective": request.objective,
                                  "tools": registry.catalog(model_only=True),
                              }))],
                    model=request.model or self.default_model, temperature=0,
                )
                plan = WorkflowPlan.model_validate(QwenOrchestrator._parse_json(raw))
                if len(plan.steps) > 8:
                    raise ValueError("Qwen plans are limited to 8 steps")
            registry.validate(plan, model_planned=model_planned)
            self.store.transition(task_id, "delegated", payload=plan.model_dump(mode="json"))
            self.store.transition(task_id, "running")
            results = await self._execute_graph(registry, task_id, request.project_id, plan)
            succeeded = all(result.status == "succeeded" for result in results)
            status = "succeeded" if succeeded else "failed"
            summary = ResultSummary(
                status=status,
                message=f"Workflow {status}: {sum(s.status == 'succeeded' for s in results)}/{len(results)} steps succeeded.",
                key_findings=[f"{s.step_id}: {s.tool} {s.status}" for s in results][:10],
            )
        except asyncio.CancelledError:
            self.store.transition(task_id, "failed", payload={"summary": {
                "status": "failed", "message": "Workflow cancelled; completed step events are retained.",
            }}, error="workflow_cancelled")
            raise
        except Exception as exc:
            # Plugins and model adapters are extension boundaries. Their failures must settle the task.
            status = "failed"
            summary = ResultSummary(status="failed", message=f"Workflow could not complete: {str(exc)[:800]}")

        result = WorkflowResult(task_id=task_id, project_id=request.project_id, status=status,
                                plan=plan, steps=results, summary=summary,
                                created_at=created_at, finished_at=utc_now())
        self.store.transition(task_id, status, payload=result.model_dump(mode="json"),
                              error=summary.message if status == "failed" else None)
        return result

    async def _execute_graph(self, registry: ToolRegistry, task_id: str, project_id: str,
                             plan: WorkflowPlan) -> list[StepResult]:
        pending = {step.step_id: step for step in plan.steps}
        settled: dict[str, StepResult] = {}
        active: dict[asyncio.Task, str] = {}

        async def run_step(step) -> StepResult:
            started = utc_now()
            self.store.append_task_event(task_id, "tool.call", step.model_dump(mode="json"))
            try:
                context = ToolContext(task_id, project_id, step.step_id, step.timeout_seconds,
                                      dependency_snapshot(settled, step.depends_on))
                outcome = await registry.execute(step.tool, step.arguments, context)
                result = StepResult(step_id=step.step_id, tool=step.tool, status=outcome.status,
                                    outcome=outcome, error=outcome.error,
                                    started_at=started, finished_at=utc_now())
            except Exception as exc:
                result = StepResult(step_id=step.step_id, tool=step.tool, status="failed",
                                    error=f"tool error: {str(exc)[:1800]}",
                                    started_at=started, finished_at=utc_now())
            self.store.append_task_event(task_id, "tool.result", result.model_dump(mode="json"))
            return result

        try:
            while pending or active:
                for step_id, step in list(pending.items()):
                    if not all(dep in settled for dep in step.depends_on):
                        continue
                    if any(settled[dep].status != "succeeded" for dep in step.depends_on):
                        now = utc_now()
                        result = StepResult(step_id=step_id, tool=step.tool, status="skipped",
                                            error="dependency did not succeed", started_at=now, finished_at=now)
                        settled[step_id] = result
                        self.store.append_task_event(task_id, "tool.result", result.model_dump(mode="json"))
                        del pending[step_id]
                    elif len(active) < plan.max_parallel:
                        active[asyncio.create_task(run_step(step))] = step_id
                        del pending[step_id]
                if active:
                    done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        settled[active.pop(task)] = task.result()
                elif pending:
                    # A skipped step may unlock an earlier-listed descendant on the next pass.
                    continue
        finally:
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
        return [settled[step.step_id] for step in plan.steps]
