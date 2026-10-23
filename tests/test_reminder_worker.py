import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app, build_task_store
from app.reminders.worker import ReminderWorker
from app.state.task_store import TaskStore


FIXED_NOW = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)


def create_due_reminder(store: TaskStore, *, channel: str = "dashboard") -> None:
    store.create_todo(
        "todo-1",
        {
            "title": "Review Friday's test results",
            "notes": "Two assertions need attention.",
            "priority": "high",
            "timezone": "America/Costa_Rica",
        },
    )
    store.create_reminder(
        "reminder-1",
        "todo-1",
        {
            "scheduled_at": (FIXED_NOW - timedelta(minutes=1)).isoformat(),
            "channel": channel,
        },
    )


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ReminderWorkerTests(unittest.TestCase):
    def test_dashboard_reminder_is_delivered_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "orchestrator.sqlite3")
            settings = Settings(data_dir=root, execution_workspace=root)
            create_due_reminder(store)
            worker = ReminderWorker(store, settings, now=lambda: FIXED_NOW)

            first_count = asyncio.run(worker.run_once())
            second_count = asyncio.run(worker.run_once())

            reminder = store.get_reminder("reminder-1")
            deliveries = store.list_reminder_deliveries(status="sent")
            self.assertEqual(first_count, 1)
            self.assertEqual(second_count, 0)
            self.assertEqual(reminder["status"], "sent")
            self.assertEqual(reminder["attempts"], 1)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0]["payload"]["title"], "Review Friday's test results")
            self.assertIsNone(deliveries[0]["acknowledged_at"])

    def test_retryable_failure_uses_backoff_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "orchestrator.sqlite3")
            settings = Settings(
                data_dir=root,
                execution_workspace=root,
                reminder_retry_seconds=10,
                reminder_max_attempts=3,
            )
            create_due_reminder(store)
            clock = MutableClock(FIXED_NOW)
            calls = 0

            async def flaky_handler(delivery):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("temporary dashboard failure")

            worker = ReminderWorker(
                store,
                settings,
                handlers={"dashboard": flaky_handler},
                now=clock,
            )

            self.assertEqual(asyncio.run(worker.run_once()), 1)
            self.assertEqual(asyncio.run(worker.run_once()), 0)
            clock.value += timedelta(seconds=10)
            self.assertEqual(asyncio.run(worker.run_once()), 1)

            reminder = store.get_reminder("reminder-1")
            deliveries = store.list_reminder_deliveries(limit=10)
            self.assertEqual(reminder["status"], "sent")
            self.assertEqual(reminder["attempts"], 2)
            self.assertEqual({item["status"] for item in deliveries}, {"failed", "sent"})

    def test_unconfigured_channel_fails_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "orchestrator.sqlite3")
            settings = Settings(data_dir=root, execution_workspace=root)
            create_due_reminder(store, channel="push")
            worker = ReminderWorker(store, settings, now=lambda: FIXED_NOW)

            self.assertEqual(asyncio.run(worker.run_once()), 1)

            reminder = store.get_reminder("reminder-1")
            delivery = store.list_reminder_deliveries(status="failed")[0]
            self.assertEqual(reminder["status"], "failed")
            self.assertEqual(reminder["attempts"], 1)
            self.assertEqual(delivery["error"], "push_channel_not_configured")

    def test_stale_claim_is_recovered_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "orchestrator.sqlite3")
            create_due_reminder(store)
            claimed = store.claim_due_reminders(now=FIXED_NOW.isoformat())

            recovered = store.recover_stale_reminder_deliveries(
                stale_before=(FIXED_NOW + timedelta(minutes=3)).isoformat(),
                retry_at=(FIXED_NOW + timedelta(minutes=3)).isoformat(),
                max_attempts=3,
            )
            retried = store.claim_due_reminders(
                now=(FIXED_NOW + timedelta(minutes=3)).isoformat(),
            )

            self.assertEqual(len(claimed), 1)
            self.assertEqual(recovered, 1)
            self.assertEqual(len(retried), 1)
            self.assertEqual(retried[0]["attempt"], 2)


class ReminderDeliveryApiTests(unittest.TestCase):
    def test_delivery_feed_and_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(data_dir=root, execution_workspace=root)
            app.dependency_overrides[get_settings] = lambda: settings
            build_task_store.cache_clear()
            store = build_task_store(str(root / "orchestrator.sqlite3"))
            create_due_reminder(store)
            delivery = store.claim_due_reminders(now=FIXED_NOW.isoformat())[0]
            store.finish_reminder_delivery(
                delivery["delivery_id"],
                succeeded=True,
                finished_at=FIXED_NOW.isoformat(),
            )
            client = TestClient(app)
            try:
                feed = client.get(
                    "/api/reminder-deliveries"
                    "?status=sent&channel=dashboard&unacknowledged_only=true"
                )
                self.assertEqual(feed.status_code, 200)
                self.assertEqual(len(feed.json()["deliveries"]), 1)

                acknowledged = client.post(
                    f"/api/reminder-deliveries/{delivery['delivery_id']}/acknowledge"
                )
                self.assertEqual(acknowledged.status_code, 200)
                self.assertIsNotNone(acknowledged.json()["acknowledged_at"])

                empty_feed = client.get(
                    "/api/reminder-deliveries"
                    "?status=sent&channel=dashboard&unacknowledged_only=true"
                )
                self.assertEqual(empty_feed.json()["deliveries"], [])
            finally:
                client.close()
                app.dependency_overrides.clear()
                build_task_store.cache_clear()


if __name__ == "__main__":
    unittest.main()
