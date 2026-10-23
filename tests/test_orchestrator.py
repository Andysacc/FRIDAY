import json
import tempfile
import unittest
from pathlib import Path

from app.agents.coding import CodingAgent
from app.execution.service import ExecutionService
from app.orchestrator.contracts import CodingTaskRequest, ExecutionPlan, ProjectCreationRequest
from app.orchestrator.qwen import OrchestrationError, QwenOrchestrator
from app.state.task_store import TaskStore


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = []

    async def chat(self, *, messages, model, temperature) -> str:
        self.calls.append({"messages": messages, "model": model, "temperature": temperature})
        return self.responses.pop(0)


class QwenOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_directory.name)
        tests_directory = self.workspace / "sample_tests"
        tests_directory.mkdir()
        (tests_directory / "test_sample.py").write_text(
            "import unittest\n\n"
            "class SampleTest(unittest.TestCase):\n"
            "    def test_true(self):\n"
            "        self.assertTrue(True)\n"
        )
        self.store = TaskStore(self.workspace / "state" / "orchestrator.sqlite3")

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    async def test_explicit_plan_delegates_and_persists_success(self) -> None:
        provider = FakeProvider([
            json.dumps({
                "status": "succeeded",
                "message": "The coding tests passed.",
                "key_findings": ["One test passed"],
            })
        ])
        orchestrator = QwenOrchestrator(
            provider=provider,
            coding_agent=CodingAgent(ExecutionService(self.workspace)),
            store=self.store,
            default_model="qwen-test",
        )
        request = CodingTaskRequest(
            task_id="explicit-task",
            objective="Run the sample backend tests",
            execution=ExecutionPlan(
                tool="python",
                action="test",
                arguments={"framework": "unittest", "targets": ["sample_tests"]},
            ),
        )

        result = await orchestrator.run_coding_task(request)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.agent_result.execution.exit_code, 0)
        self.assertEqual(len(provider.calls), 1)
        persisted = self.store.get_task("explicit-task")
        self.assertEqual(persisted["status"], "succeeded")
        self.assertEqual(persisted["attempts"], 1)
        self.assertEqual(persisted["plan"]["tool"], "python")

    async def test_qwen_plan_is_validated_before_delegation(self) -> None:
        provider = FakeProvider([
            json.dumps({
                "schema_version": "1.0",
                "agent": "coding",
                "execution": {
                    "tool": "python",
                    "action": "test",
                    "arguments": {"framework": "unittest", "targets": ["sample_tests"]},
                    "timeout_seconds": 10,
                },
            }),
            json.dumps({
                "status": "succeeded",
                "message": "The coding tests passed.",
                "key_findings": [],
            }),
        ])
        orchestrator = QwenOrchestrator(
            provider=provider,
            coding_agent=CodingAgent(ExecutionService(self.workspace)),
            store=self.store,
            default_model="qwen-test",
        )

        result = await orchestrator.run_coding_task(
            CodingTaskRequest(task_id="planned-task", objective="Validate the backend tests")
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.plan.action, "test")
        self.assertEqual(len(provider.calls), 2)

    async def test_invalid_qwen_plan_fails_closed(self) -> None:
        provider = FakeProvider([
            '{"schema_version":"1.0","agent":"coding","execution":'
            '{"tool":"python","action":"shell","arguments":{}}}'
        ])
        orchestrator = QwenOrchestrator(
            provider=provider,
            coding_agent=CodingAgent(ExecutionService(self.workspace)),
            store=self.store,
            default_model="qwen-test",
        )

        result = await orchestrator.run_coding_task(
            CodingTaskRequest(task_id="invalid-task", objective="Run an arbitrary command")
        )

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.agent_result)
        self.assertIn("No unvalidated operation", result.summary.key_findings[0])
        self.assertEqual(self.store.get_task("invalid-task")["attempts"], 0)

    async def test_qwen_cannot_self_authorize_workspace_mutation(self) -> None:
        provider = FakeProvider([
            json.dumps({
                "schema_version": "1.0",
                "agent": "coding",
                "execution": {
                    "tool": "workspace",
                    "action": "scaffold",
                    "arguments": {
                        "project_id": "unauthorized",
                        "name": "Unauthorized",
                        "description": "Must not be created.",
                        "milestone": "None",
                        "objective": "Attempt a mutation",
                        "success_criteria": ["No files are written"],
                    },
                },
            })
        ])
        orchestrator = QwenOrchestrator(
            provider=provider,
            coding_agent=CodingAgent(ExecutionService(self.workspace)),
            store=self.store,
            default_model="qwen-test",
        )

        result = await orchestrator.run_coding_task(
            CodingTaskRequest(task_id="mutation-task", objective="Create a workspace")
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("No unvalidated operation", result.summary.key_findings[0])
        self.assertFalse((self.workspace / "projects").exists())

    async def test_project_creation_plans_registers_and_activates_without_writing_files(self) -> None:
        provider = FakeProvider([
            json.dumps({
                "project_id": "research-lab",
                "name": "Research Lab",
                "description": "A reproducible local research pipeline.",
                "milestone": "Define the first experiment",
                "success_criteria": ["Experiment is reproducible", "Results are documented"],
            })
        ])
        orchestrator = QwenOrchestrator(
            provider=provider,
            coding_agent=CodingAgent(ExecutionService(self.workspace)),
            store=self.store,
            default_model="qwen-test",
        )

        result = await orchestrator.create_project(
            ProjectCreationRequest(objective="Build a reproducible research pipeline", source="chat")
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.project["name"], "Research Lab")
        self.assertEqual(self.store.get_active_project()["project_id"], "research-lab")
        self.assertFalse((self.workspace / "projects").exists())
        self.assertEqual(len(provider.calls), 1)

    async def test_project_creation_scaffolds_after_explicit_authorization(self) -> None:
        provider = FakeProvider([
            json.dumps({
                "project_id": "research-lab",
                "name": "Research Lab",
                "description": "A reproducible local research pipeline.",
                "milestone": "Define the first experiment",
                "success_criteria": ["Experiment is reproducible", "Results are documented"],
            }),
            json.dumps({
                "status": "succeeded",
                "message": "The starter workspace was created.",
                "key_findings": ["Two fixed starter files were written"],
            }),
        ])
        orchestrator = QwenOrchestrator(
            provider=provider,
            coding_agent=CodingAgent(ExecutionService(self.workspace)),
            store=self.store,
            default_model="qwen-test",
        )

        result = await orchestrator.create_project(
            ProjectCreationRequest(
                objective="Build a reproducible research pipeline",
                source="chat",
                scaffold=True,
            )
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.scaffold_result.status, "succeeded")
        self.assertTrue((self.workspace / "projects" / "research-lab" / "README.md").is_file())
        tasks = self.store.list_project_tasks("research-lab")
        self.assertEqual(tasks[0]["plan"]["tool"], "workspace")

    async def test_invalid_project_blueprint_fails_before_registration(self) -> None:
        provider = FakeProvider(['{"project_id":"bad/id","name":"Bad"}'])
        orchestrator = QwenOrchestrator(
            provider=provider,
            coding_agent=CodingAgent(ExecutionService(self.workspace)),
            store=self.store,
            default_model="qwen-test",
        )

        with self.assertRaises(OrchestrationError):
            await orchestrator.create_project(ProjectCreationRequest(objective="Create something unsafe"))

        self.assertEqual(self.store.list_projects(), [])

    async def test_duplicate_generated_project_ids_receive_a_suffix(self) -> None:
        blueprint = {
            "project_id": "research-lab",
            "name": "Research Lab",
            "description": "A reproducible local research pipeline.",
            "milestone": "Define the first experiment",
            "success_criteria": ["Experiment is reproducible"],
        }
        provider = FakeProvider([json.dumps(blueprint), json.dumps(blueprint)])
        orchestrator = QwenOrchestrator(
            provider=provider,
            coding_agent=CodingAgent(ExecutionService(self.workspace)),
            store=self.store,
            default_model="qwen-test",
        )

        first = await orchestrator.create_project(ProjectCreationRequest(objective="First lab"))
        second = await orchestrator.create_project(ProjectCreationRequest(objective="Second lab"))

        self.assertEqual(first.blueprint.project_id, "research-lab")
        self.assertEqual(second.blueprint.project_id, "research-lab-2")
        self.assertEqual(len(self.store.list_projects()), 2)


class TaskRecoveryTests(unittest.TestCase):
    def test_in_progress_task_is_failed_on_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orchestrator.sqlite3"
            store = TaskStore(path)
            store.create_task(
                task_id="interrupted-task",
                project_id="friday",
                objective="Run tests",
                request={"objective": "Run tests"},
            )
            store.transition("interrupted-task", "planning")
            store.transition("interrupted-task", "running")

            restarted_store = TaskStore(path)

            task = restarted_store.get_task("interrupted-task")
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["error"], "process_interrupted")


if __name__ == "__main__":
    unittest.main()
