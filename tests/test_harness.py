import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.execution.service import ExecutionService
from app.harness.builtins import build_registry
from app.harness.contracts import Contract, ToolOutcome, WorkflowPlan, WorkflowRequest
from app.harness.registry import ToolPlugin, ToolRegistry
from app.harness.runner import HarnessRunner
from app.main import app, build_task_store
from app.orchestrator.contracts import ExecutionRequest
from app.state.task_store import TaskStore


class Provider:
    def __init__(self, response=""):
        self.response = response
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class ValueArguments(Contract):
    value: int = 0


def step(step_id, *, depends_on=None, tool="sample.value", value=0, timeout_seconds=30):
    return dict(step_id=step_id, tool=tool, arguments={"value": value},
                depends_on=depends_on or [], timeout_seconds=timeout_seconds)


class WorkflowContractTests(unittest.TestCase):
    def test_invalid_graphs_and_limits_rejected(self):
        graphs = [
            [step("same"), step("same")],
            [step("a", depends_on=["missing"])],
            [step("a", depends_on=["a"])],
            [step("a", depends_on=["b"]), step("b", depends_on=["a"])],
            [step("a"), step("b", depends_on=["a", "a"])],
            [],
            [step(f"s{i}") for i in range(33)],
        ]
        for graph in graphs:
            with self.subTest(graph=graph), self.assertRaises(ValidationError):
                WorkflowPlan(steps=graph)
        with self.assertRaises(ValidationError):
            WorkflowPlan(steps=[step("a")], max_parallel=5)
        with self.assertRaises(ValidationError):
            WorkflowRequest(plan=WorkflowPlan(steps=[step("a")]), workflow_id="saved")


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = TaskStore(self.root / "state.sqlite3")
        self.registry = ToolRegistry()
        self.provider = Provider()

    def tearDown(self):
        self.directory.cleanup()

    def runner(self):
        return HarnessRunner(registry=self.registry, store=self.store,
                             provider=self.provider, default_model="qwen-test")

    async def test_parallel_dependencies_and_durable_events(self):
        active = 0
        peak = 0
        started = set()
        both_started = asyncio.Event()

        async def execute(args, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.add(context.step_id)
            if {"a", "b"} <= started:
                both_started.set()
            if context.step_id in {"a", "b"}:
                await asyncio.wait_for(both_started.wait(), 2)
            else:
                self.assertEqual(set(context.dependencies), {"a", "b"})
                self.assertTrue(all(s.status == "succeeded" for s in context.dependencies.values()))
                # A plugin can change its own dependency copies, never recorded predecessor results.
                context.dependencies["a"].outcome.output["value"] = 999
            active -= 1
            return ToolOutcome(status="succeeded", output={"value": args.value})

        self.registry.register(ToolPlugin("sample.value", "sample", ValueArguments, execute))
        result = await self.runner().run(WorkflowRequest(
            task_id="parallel", project_id="lab", plan=WorkflowPlan(steps=[
                step("c", depends_on=["a", "b"]), step("a", value=1), step("b", value=2),
            ], max_parallel=2),
        ))
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(peak, 2)
        self.assertEqual([s.step_id for s in result.steps], ["c", "a", "b"])
        self.assertEqual(result.steps[1].outcome.output["value"], 1)
        self.assertEqual(self.provider.calls, [])
        task = self.store.get_task("parallel")
        self.assertEqual(task["task_type"], "workflow")
        self.assertEqual(task["result"]["steps"], result.model_dump(mode="json")["steps"])
        events = self.store.list_task_events("parallel")
        self.assertEqual(sum(e["event"] == "tool.call" for e in events), 3)
        self.assertEqual(sum(e["event"] == "tool.result" for e in events), 3)
        self.assertEqual(self.store.list_completed_tasks()[0]["task_id"], "parallel")

    async def test_concurrency_never_exceeds_limit(self):
        active = peak = 0

        async def execute(args, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolOutcome(status="succeeded")

        self.registry.register(ToolPlugin("sample.value", "sample", ValueArguments, execute))
        result = await self.runner().run(WorkflowRequest(plan=WorkflowPlan(
            steps=[step(f"s{i}") for i in range(8)], max_parallel=2,
        )))
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(peak, 2)

    async def test_failures_skip_descendants_but_independent_branch_finishes(self):
        executed = []

        async def execute(args, context):
            executed.append(context.step_id)
            if context.step_id == "bad":
                raise RuntimeError("broken plugin")
            return ToolOutcome(status="succeeded")

        self.registry.register(ToolPlugin("sample.value", "sample", ValueArguments, execute))
        result = await self.runner().run(WorkflowRequest(plan=WorkflowPlan(steps=[
            step("grandchild", depends_on=["child"]), step("child", depends_on=["bad"]),
            step("bad"), step("independent"),
        ])))
        self.assertEqual(result.status, "failed")
        self.assertEqual([s.status for s in result.steps], ["skipped", "skipped", "failed", "succeeded"])
        self.assertEqual(set(executed), {"bad", "independent"})

    async def test_full_plan_preflight_prevents_partial_execution(self):
        executed = []

        async def execute(args, context):
            executed.append(context.step_id)
            return ToolOutcome(status="succeeded")

        self.registry.register(ToolPlugin("sample.value", "sample", ValueArguments, execute))
        for bad in [step("bad", tool="unknown.tool"),
                    dict(step("bad"), arguments={"unexpected": True})]:
            result = await self.runner().run(WorkflowRequest(plan=WorkflowPlan(steps=[step("good"), bad])))
            self.assertEqual(result.status, "failed")
        self.assertEqual(executed, [])

    async def test_qwen_plan_uses_catalog_and_preserves_authoritative_failure(self):
        async def execute(args, context):
            return ToolOutcome(status="failed", error="real failure")

        self.registry.register(ToolPlugin("sample.value", "sample", ValueArguments, execute, model_selectable=True))
        self.provider.response = json.dumps({"steps": [step("a")]})
        result = await self.runner().run(WorkflowRequest(objective="Check all components"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.summary.status, "failed")
        self.assertEqual(len(self.provider.calls), 1)
        prompt = self.provider.calls[0]["messages"][1].content
        self.assertIn("arguments_schema", prompt)
        self.assertEqual(self.provider.calls[0]["model"], "qwen-test")

    async def test_untrusted_plans_cannot_select_restricted_plugins(self):
        executed = []

        async def execute(args, context):
            executed.append(1)
            return ToolOutcome(status="succeeded")

        self.registry.register(ToolPlugin("sample.value", "sample", ValueArguments, execute))
        self.provider.response = json.dumps({"steps": [step("a")]})
        result = await self.runner().run(WorkflowRequest(objective="Use a restricted plugin"))
        self.assertEqual(result.status, "failed")
        self.assertIn("cannot authorize", result.summary.message)
        self.assertEqual(executed, [])
        self.assertEqual(self.registry.catalog(model_only=True), [])

    async def test_bad_model_output_and_oversized_plans_fail_without_tools(self):
        for response in ["not json", '["wrong shape"]', json.dumps({"steps": [step(f"s{i}") for i in range(9)]})]:
            self.provider.response = response
            result = await self.runner().run(WorkflowRequest(objective="Inspect the project"))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.steps, [])
            self.assertEqual(self.store.get_task(result.task_id)["status"], "failed")

    async def test_timeout_skips_dependent_and_cleans_up_plugin(self):
        cleaned = asyncio.Event()

        async def execute(args, context):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        self.registry.register(ToolPlugin("sample.value", "sample", ValueArguments, execute))
        result = await self.runner().run(WorkflowRequest(plan=WorkflowPlan(steps=[
            step("slow", timeout_seconds=1), step("child", depends_on=["slow"]),
        ])))
        self.assertEqual([s.status for s in result.steps], ["timed_out", "skipped"])
        self.assertTrue(cleaned.is_set())

    async def test_cancellation_settles_task_and_awaits_children(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def execute(args, context):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        self.registry.register(ToolPlugin("sample.value", "sample", ValueArguments, execute))
        task = asyncio.create_task(self.runner().run(WorkflowRequest(
            task_id="cancelled", plan=WorkflowPlan(steps=[step("slow")]),
        )))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.store.get_task("cancelled")["error"], "workflow_cancelled")

    async def test_real_builtin_compile_and_preflight_path_guards(self):
        self.registry = build_registry(ExecutionService(self.root))
        (self.root / "sample.py").write_text("value = 42\n")
        result = await self.runner().run(WorkflowRequest(plan=WorkflowPlan(steps=[
            dict(step("compile", tool="python.compile"), arguments={"targets": ["sample.py"]}),
        ])))
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.steps[0].outcome.execution.exit_code, 0)
        (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
        for path in ["../escape", "/tmp", "--help", "./--help", "outside/file.py"]:
            with self.subTest(path=path):
                result = await self.runner().run(WorkflowRequest(plan=WorkflowPlan(steps=[
                    {"step_id": "bad", "tool": "python.compile", "arguments": {"targets": [path]}},
                ])))
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.steps, [])

    async def test_saved_plan_is_snapshotted_and_registry_lifecycle_is_explicit(self):
        async def execute(args, context):
            self.registry.unregister("sample.value")
            return ToolOutcome(status="succeeded", output={"value": args.value})

        plugin = ToolPlugin("sample.value", "sample", ValueArguments, execute)
        self.registry.register(plugin)
        with self.assertRaises(ValueError):
            self.registry.register(plugin)
        self.store.save_workflow("sample", {"name": "Sample", "plan": {"steps": [step("a", value=7)]}})
        result = await self.runner().run(WorkflowRequest(workflow_id="sample"))
        self.assertEqual(result.steps[0].outcome.output["value"], 7)
        self.assertEqual(self.registry.catalog(), [])
        self.assertEqual(result.plan.steps[0].tool, "sample.value")

    async def test_restart_retains_completed_step_events(self):
        self.store.create_task(task_id="restart", project_id="friday", objective="Inspect",
                               request={}, task_type="workflow")
        self.store.transition("restart", "running")
        self.store.append_task_event("restart", "tool.result", {"step_id": "finished", "status": "succeeded"})
        recovered = TaskStore(self.root / "state.sqlite3")
        self.assertEqual(recovered.get_task("restart")["error"], "process_interrupted")
        self.assertEqual(recovered.list_task_events("restart")[-2]["payload"]["step_id"], "finished")

    async def test_execution_cancellation_terminates_subprocess(self):
        started = asyncio.Event()
        process = None
        original = asyncio.create_subprocess_exec

        async def capture(*args, **kwargs):
            nonlocal process
            process = await original(*args, **kwargs)
            started.set()
            return process

        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_slow.py").write_text(
            "import unittest, time\nclass Slow(unittest.TestCase):\n def test_slow(self): time.sleep(30)\n"
        )
        with patch("app.execution.service.asyncio.create_subprocess_exec", side_effect=capture):
            task = asyncio.create_task(ExecutionService(self.root).execute(ExecutionRequest(
                task_id="process", tool="python", action="test",
            )))
            await asyncio.wait_for(started.wait(), 2)
            # Allow pipe readers to initialize before cancellation.
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(process.returncode)


class HarnessApiTests(unittest.TestCase):
    def test_catalog_templates_definition_crud_runs_events_and_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text("value = 1\n")
            settings = Settings(data_dir=root, execution_workspace=root)
            app.dependency_overrides[get_settings] = lambda: settings
            build_task_store.cache_clear()
            try:
                client = TestClient(app)
                catalog = client.get("/api/harness/tools").json()["tools"]
                self.assertEqual(len(catalog), 9)
                self.assertNotIn("workspace.scaffold", [tool["name"] for tool in catalog])
                self.assertIn("code-health", client.get("/api/harness/templates").json()["templates"])
                definition = {"name": "Syntax", "plan": {"steps": [
                    {"step_id": "syntax", "tool": "python.compile", "arguments": {"targets": ["sample.py"]}},
                ]}}
                self.assertEqual(client.put("/api/harness/workflows/syntax", json=definition).status_code, 200)
                self.assertEqual(client.get("/api/harness/workflows").json()["workflows"][0]["name"], "Syntax")
                request = {"task_id": "api-workflow", "workflow_id": "syntax"}
                response = client.post("/api/harness/runs", json=request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "succeeded")
                self.assertEqual(client.post("/api/harness/runs", json=request).status_code, 409)
                task = client.get("/api/orchestrator/tasks/api-workflow").json()
                self.assertEqual(task["task_type"], "workflow")
                events = client.get("/api/orchestrator/tasks/api-workflow/events").json()["events"]
                self.assertIn("tool.result", [event["event"] for event in events])
                self.assertEqual(client.get("/api/orchestrator/tasks/missing/events").status_code, 404)
                self.assertEqual(client.delete("/api/harness/workflows/syntax").status_code, 200)
                self.assertEqual(client.get("/api/harness/workflows/syntax").status_code, 404)
                self.assertEqual(client.delete("/api/harness/workflows/syntax").status_code, 404)
                self.assertEqual(client.post("/api/harness/runs", json={"workflow_id": "syntax"}).json()["status"], "failed")
                definition["plan"]["steps"][0]["tool"] = "workspace.scaffold"
                self.assertEqual(client.put("/api/harness/workflows/unsafe", json=definition).status_code, 422)
                self.assertEqual(client.get("/api/harness/workflows/unsafe").status_code, 404)
            finally:
                app.dependency_overrides.clear()
                build_task_store.cache_clear()
