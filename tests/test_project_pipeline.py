import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app, build_task_store
from app.orchestrator.contracts import OrchestrationResult, ResultSummary
from app.state.task_store import TaskStore


class ProjectStoreTests(unittest.TestCase):
    def test_project_registry_tracks_active_project_and_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "orchestrator.sqlite3")
            state = {
                "name": "Quant Lab",
                "description": "Research pipeline",
                "progress": 50,
                "milestone": "Run validation",
                "status": "on-track",
                "pipeline_stage": 2,
            }
            store.upsert_project("quant-lab", state)
            active = store.set_active_project("quant-lab")
            store.create_task(
                task_id="test-task",
                project_id="quant-lab",
                objective="Run tests",
                request={"objective": "Run tests"},
            )
            store.transition("test-task", "planning")
            store.transition(
                "test-task",
                "succeeded",
                payload={
                    "summary": {
                        "status": "succeeded",
                        "message": "All tests passed.",
                        "key_findings": ["One test passed"],
                    }
                },
            )

            self.assertEqual(active["name"], "Quant Lab")
            self.assertEqual(store.get_active_project()["project_id"], "quant-lab")
            self.assertEqual(store.list_projects()[0]["pipeline_stage"], 2)
            self.assertEqual(store.list_project_tasks("quant-lab")[0]["task_id"], "test-task")
            completion = store.list_completed_tasks()[0]
            self.assertEqual(completion["task_id"], "test-task")
            self.assertEqual(completion["project_name"], "Quant Lab")
            self.assertEqual(completion["result"]["summary"]["message"], "All tests passed.")

    def test_project_deletion_removes_tasks_events_and_active_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "orchestrator.sqlite3")
            store.upsert_project(
                "delete-me",
                {
                    "name": "Delete Me",
                    "description": "Temporary project",
                    "progress": 25,
                    "milestone": "Cleanup",
                    "status": "on-track",
                    "pipeline_stage": 1,
                },
            )
            store.set_active_project("delete-me")
            store.create_task(
                task_id="delete-task",
                project_id="delete-me",
                objective="Run tests",
                request={"objective": "Run tests"},
            )
            store.transition("delete-task", "failed", payload={"summary": {"message": "Failed"}})

            result = store.delete_project("delete-me")

            self.assertEqual(result["deleted_tasks"], 1)
            self.assertIsNone(store.get_project("delete-me"))
            self.assertIsNone(store.get_task("delete-task"))
            self.assertIsNone(store.get_active_project())
            self.assertEqual(store.list_completed_tasks(), [])


class FakeSuccessfulOrchestrator:
    async def run_coding_task(self, request):
        now = datetime.now(timezone.utc)
        return OrchestrationResult(
            task_id="pipeline-test-task",
            project_id=request.project_id,
            status="succeeded",
            plan=request.execution,
            summary=ResultSummary(
                status="succeeded",
                message="All tests passed.",
                key_findings=["Test suite passed"],
            ),
            created_at=now,
            finished_at=now,
        )


class ProjectTestingApiTests(unittest.TestCase):
    def test_orchestrated_project_endpoint_accepts_explicit_blueprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), execution_workspace=Path(directory))
            app.dependency_overrides[get_settings] = lambda: settings
            build_task_store.cache_clear()
            client = TestClient(app)
            try:
                response = client.post(
                    "/api/orchestrator/projects",
                    json={
                        "objective": "Build a local research pipeline",
                        "source": "api",
                        "blueprint": {
                            "project_id": "research-lab",
                            "name": "Research Lab",
                            "description": "A reproducible local research pipeline.",
                            "milestone": "Define the first experiment",
                            "success_criteria": [
                                "Experiment is reproducible",
                                "Results are documented",
                            ],
                        },
                    },
                )

                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["status"], "succeeded")
                self.assertEqual(payload["project"]["project_id"], "research-lab")
                active = client.get("/api/projects/active").json()["project"]
                self.assertEqual(active["project_id"], "research-lab")
                self.assertEqual(len(active["success_criteria"]), 2)
            finally:
                app.dependency_overrides.clear()
                build_task_store.cache_clear()

    def test_successful_test_advances_execution_project_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), execution_workspace=Path(directory))
            app.dependency_overrides[get_settings] = lambda: settings
            build_task_store.cache_clear()
            client = TestClient(app)
            try:
                project_response = client.put(
                    "/api/projects/quant-lab",
                    json={
                        "name": "Quant Lab",
                        "description": "Research pipeline",
                        "progress": 50,
                        "milestone": "Run validation",
                        "status": "on-track",
                        "pipeline_stage": 2,
                    },
                )
                self.assertEqual(project_response.status_code, 200)

                with patch("app.main.build_orchestrator", return_value=FakeSuccessfulOrchestrator()):
                    test_response = client.post(
                        "/api/projects/quant-lab/tests",
                        json={"source": "chat", "framework": "unittest", "targets": ["tests"]},
                    )

                self.assertEqual(test_response.status_code, 200)
                payload = test_response.json()
                self.assertEqual(payload["result"]["status"], "succeeded")
                self.assertEqual(payload["project"]["pipeline_stage"], 3)
                self.assertEqual(payload["project"]["progress"], 75)
            finally:
                app.dependency_overrides.clear()
                build_task_store.cache_clear()

    def test_completion_feed_returns_finished_orchestrator_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), execution_workspace=Path(directory))
            app.dependency_overrides[get_settings] = lambda: settings
            build_task_store.cache_clear()
            store = build_task_store(str(settings.data_dir / "orchestrator.sqlite3"))
            store.upsert_project(
                "quant-lab",
                {
                    "name": "Quant Lab",
                    "description": "Research pipeline",
                    "progress": 50,
                    "milestone": "Run validation",
                    "status": "on-track",
                    "pipeline_stage": 2,
                },
            )
            store.create_task(
                task_id="completed-task",
                project_id="quant-lab",
                objective="Run tests",
                request={"objective": "Run tests"},
            )
            store.transition(
                "completed-task",
                "succeeded",
                payload={"summary": {"status": "succeeded", "message": "Tests passed."}},
            )
            client = TestClient(app)
            try:
                response = client.get("/api/orchestrator/completions?limit=10")
                self.assertEqual(response.status_code, 200)
                completion = response.json()["completions"][0]
                self.assertEqual(completion["task_id"], "completed-task")
                self.assertEqual(completion["project_name"], "Quant Lab")
            finally:
                app.dependency_overrides.clear()
                build_task_store.cache_clear()

    def test_delete_project_endpoint_removes_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), execution_workspace=Path(directory))
            app.dependency_overrides[get_settings] = lambda: settings
            build_task_store.cache_clear()
            client = TestClient(app)
            try:
                create_response = client.put(
                    "/api/projects/delete-me",
                    json={
                        "name": "Delete Me",
                        "description": "Temporary project",
                        "progress": 0,
                        "milestone": "Cleanup",
                        "status": "on-track",
                        "pipeline_stage": 0,
                    },
                )
                self.assertEqual(create_response.status_code, 200)
                activate_response = client.post("/api/projects/delete-me/active")
                self.assertEqual(activate_response.status_code, 200)

                delete_response = client.delete("/api/projects/delete-me")

                self.assertEqual(delete_response.status_code, 200)
                self.assertTrue(delete_response.json()["deleted"])
                self.assertEqual(client.get("/api/projects/delete-me").status_code, 404)
                self.assertEqual(client.get("/api/projects").json()["projects"], [])
                self.assertIsNone(client.get("/api/projects/active").json()["project"])
            finally:
                app.dependency_overrides.clear()
                build_task_store.cache_clear()


if __name__ == "__main__":
    unittest.main()
