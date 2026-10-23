import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app, build_task_store
from app.state.task_store import TaskStore


class TodoStoreTests(unittest.TestCase):
    def test_completing_todo_cancels_pending_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "orchestrator.sqlite3")
            todo = store.create_todo(
                "todo-1",
                {
                    "title": "Review test failures",
                    "priority": "high",
                    "timezone": "America/Costa_Rica",
                    "due_at": "2026-08-08T15:00:00Z",
                },
            )
            reminder = store.create_reminder(
                "reminder-1",
                todo["todo_id"],
                {
                    "scheduled_at": "2026-08-08T14:30:00Z",
                    "channel": "push",
                },
            )

            completed = store.update_todo(todo["todo_id"], {"status": "completed"})
            reminders = store.list_reminders(todo["todo_id"])

            self.assertEqual(reminder["status"], "pending")
            self.assertEqual(completed["status"], "completed")
            self.assertIsNotNone(completed["completed_at"])
            self.assertEqual(completed["version"], 2)
            self.assertEqual(reminders[0]["status"], "cancelled")

    def test_deleting_todo_cascades_to_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "orchestrator.sqlite3")
            store.create_todo("todo-1", {"title": "Temporary", "timezone": "UTC"})
            store.create_reminder(
                "reminder-1",
                "todo-1",
                {"scheduled_at": "2026-08-08T14:30:00Z", "channel": "dashboard"},
            )

            self.assertTrue(store.delete_todo("todo-1"))
            self.assertIsNone(store.get_todo("todo-1"))
            self.assertIsNone(store.get_reminder("reminder-1"))


class TodoApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.settings = Settings(data_dir=root, execution_workspace=root)
        app.dependency_overrides[get_settings] = lambda: self.settings
        build_task_store.cache_clear()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        build_task_store.cache_clear()
        self.temporary_directory.cleanup()

    def test_todo_and_reminder_crud(self) -> None:
        create = self.client.post(
            "/api/todos",
            json={
                "title": "  Run the Friday test suite  ",
                "priority": "critical",
                "due_at": "2026-08-08T09:00:00-06:00",
                "timezone": "America/Costa_Rica",
            },
        )

        self.assertEqual(create.status_code, 201)
        todo = create.json()
        self.assertEqual(todo["title"], "Run the Friday test suite")
        self.assertEqual(todo["due_at"], "2026-08-08T15:00:00Z")

        reminder_create = self.client.post(
            f"/api/todos/{todo['todo_id']}/reminders",
            json={
                "scheduled_at": "2026-08-08T08:30:00-06:00",
                "channel": "push",
            },
        )
        self.assertEqual(reminder_create.status_code, 201)
        reminder = reminder_create.json()

        update = self.client.patch(
            f"/api/todos/{todo['todo_id']}",
            json={"status": "completed"},
        )
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["status"], "completed")

        reminder_list = self.client.get(f"/api/todos/{todo['todo_id']}/reminders")
        self.assertEqual(reminder_list.status_code, 200)
        self.assertEqual(reminder_list.json()["reminders"][0]["status"], "cancelled")

        invalid_reactivation = self.client.patch(
            f"/api/reminders/{reminder['reminder_id']}",
            json={"status": "pending"},
        )
        self.assertEqual(invalid_reactivation.status_code, 409)

        reopen = self.client.patch(
            f"/api/todos/{todo['todo_id']}",
            json={"status": "open"},
        )
        self.assertEqual(reopen.status_code, 200)
        self.assertIsNone(reopen.json()["completed_at"])

        reschedule = self.client.patch(
            f"/api/reminders/{reminder['reminder_id']}",
            json={"status": "pending", "scheduled_at": "2026-08-08T15:30:00Z"},
        )
        self.assertEqual(reschedule.status_code, 200)
        self.assertEqual(reschedule.json()["status"], "pending")

        listing = self.client.get("/api/todos?status=open")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()["todos"]), 1)

        delete = self.client.delete(f"/api/todos/{todo['todo_id']}")
        self.assertEqual(delete.status_code, 200)
        self.assertTrue(delete.json()["deleted"])
        self.assertEqual(self.client.get(f"/api/todos/{todo['todo_id']}").status_code, 404)
        self.assertEqual(
            self.client.patch(
                f"/api/reminders/{reminder['reminder_id']}",
                json={"status": "cancelled"},
            ).status_code,
            404,
        )

    def test_todo_validation_and_project_reference(self) -> None:
        naive_due = self.client.post(
            "/api/todos",
            json={"title": "Invalid date", "due_at": "2026-08-08T09:00:00"},
        )
        missing_project = self.client.post(
            "/api/todos",
            json={"title": "Unknown project", "project_id": "missing"},
        )

        self.assertEqual(naive_due.status_code, 422)
        self.assertEqual(missing_project.status_code, 404)


if __name__ == "__main__":
    unittest.main()
