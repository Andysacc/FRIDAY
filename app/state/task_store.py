import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from app.orchestrator.contracts import TaskStatus, utc_now


class TaskStore:
    """Durable task/event state used by the orchestrator and recovery logic."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()
        self.recover_interrupted_tasks()

    def create_task(
        self,
        *,
        task_id: str,
        project_id: str,
        objective: str,
        request: dict[str, Any],
        task_type: str = "coding",
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO projects (project_id, state_json, created_at, updated_at) "
                "VALUES (?, '{}', ?, ?) ON CONFLICT(project_id) DO UPDATE SET updated_at=excluded.updated_at",
                (project_id, now, now),
            )
            connection.execute(
                "INSERT INTO tasks "
                "(task_id, project_id, task_type, objective, status, request_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                (task_id, project_id, task_type, objective, self._json(request), now, now),
            )
            self._insert_event(connection, task_id, "queued", {"request": request}, now)

    def upsert_project(self, project_id: str, state: dict[str, Any]) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            created_at = row[0] if row else now
            connection.execute(
                "INSERT INTO projects (project_id, state_json, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                (project_id, self._json(state), created_at, now),
            )
        return {"project_id": project_id, **state, "created_at": created_at, "updated_at": now}

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id, state_json, created_at, updated_at FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        projects: list[dict[str, Any]] = []
        for row in rows:
            state = json.loads(row[1]) if row[1] else {}
            if not state.get("name"):
                continue
            projects.append({
                "project_id": row[0],
                **state,
                "created_at": row[2],
                "updated_at": row[3],
            })
        return projects

    def set_active_project(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(f"unknown project: {project_id}")
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO system_state (key, value, updated_at) VALUES ('active_project_id', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (project_id, now),
            )
        return project

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT project_id, state_json, created_at, updated_at FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None or not row[1]:
            return None
        state = json.loads(row[1])
        if not state.get("name"):
            return None
        return {"project_id": row[0], **state, "created_at": row[2], "updated_at": row[3]}

    def get_active_project(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM system_state WHERE key = 'active_project_id'"
            ).fetchone()
        return self.get_project(row[0]) if row else None

    def delete_project(self, project_id: str) -> dict[str, int | str]:
        """Delete one project and its task history as a single transaction."""

        with self._lock, self._connect() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(f"unknown project: {project_id}")

            task_count = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM task_events WHERE task_id IN "
                "(SELECT task_id FROM tasks WHERE project_id = ?)",
                (project_id,),
            )
            connection.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            connection.execute(
                "DELETE FROM system_state WHERE key = 'active_project_id' AND value = ?",
                (project_id,),
            )
        return {"project_id": project_id, "deleted_tasks": task_count}

    def list_project_tasks(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            task = dict(row)
            for field in ("request_json", "plan_json", "result_json"):
                if task[field]:
                    task[field.removesuffix("_json")] = json.loads(task.pop(field))
                else:
                    task.pop(field)
            tasks.append(task)
        return tasks

    def list_completed_tasks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return the latest terminal task results for dashboard completion reporting."""

        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT tasks.*, projects.state_json AS project_state_json "
                "FROM tasks JOIN projects ON projects.project_id = tasks.project_id "
                "WHERE tasks.status IN ('succeeded', 'failed') "
                "ORDER BY tasks.updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        tasks: list[dict[str, Any]] = []
        for row in rows:
            task = dict(row)
            project_state = json.loads(task.pop("project_state_json") or "{}")
            task["project_name"] = project_state.get("name") or task["project_id"]
            for field in ("request_json", "plan_json", "result_json"):
                if task[field]:
                    task[field.removesuffix("_json")] = json.loads(task.pop(field))
                else:
                    task.pop(field)
            tasks.append(task)
        return tasks

    def transition(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        updates = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, now]
        if status == "running":
            updates.append("attempts = attempts + 1")
        if payload is not None:
            if status == "delegated":
                updates.append("plan_json = ?")
            elif status in {"succeeded", "failed"}:
                updates.append("result_json = ?")
            if status in {"delegated", "succeeded", "failed"}:
                values.append(self._json(payload))
        if error is not None:
            updates.append("error = ?")
            values.append(error)
        values.append(task_id)

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown task: {task_id}")
            self._insert_event(connection, task_id, status, payload or {"error": error}, now)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("request_json", "plan_json", "result_json"):
            if result[field]:
                result[field.removesuffix("_json")] = json.loads(result.pop(field))
            else:
                result.pop(field)
        return result

    def create_todo(self, todo_id: str, todo: dict[str, Any]) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO todos "
                "(todo_id, title, notes, project_id, status, priority, due_at, timezone, "
                "recurrence_rule, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (
                    todo_id,
                    todo["title"],
                    todo.get("notes", ""),
                    todo.get("project_id"),
                    todo.get("priority", "normal"),
                    todo.get("due_at"),
                    todo.get("timezone", "UTC"),
                    todo.get("recurrence_rule"),
                    now,
                    now,
                ),
            )
        result = self.get_todo(todo_id)
        if result is None:  # pragma: no cover - protects against an unexpected SQLite failure
            raise RuntimeError("todo was not persisted")
        return result

    def list_todos(self, *, status: str = "open", limit: int = 200) -> list[dict[str, Any]]:
        where = "" if status == "all" else "WHERE status = ?"
        parameters: list[Any] = [] if status == "all" else [status]
        parameters.append(limit)
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM todos {where} "  # noqa: S608 - where is selected from fixed strings above
                "ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, "
                "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_todo(self, todo_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM todos WHERE todo_id = ?", (todo_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_todo(self, todo_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "title",
            "notes",
            "project_id",
            "status",
            "priority",
            "due_at",
            "timezone",
            "recurrence_rule",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"unsupported todo fields: {', '.join(sorted(invalid))}")

        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT status, completed_at FROM todos WHERE todo_id = ?",
                (todo_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"unknown todo: {todo_id}")

            assignments: list[str] = []
            values: list[Any] = []
            for field, value in changes.items():
                assignments.append(f"{field} = ?")
                values.append(value)

            next_status = changes.get("status", existing[0])
            if next_status == "completed" and existing[0] != "completed":
                assignments.append("completed_at = ?")
                values.append(now)
                connection.execute(
                    "UPDATE reminders SET status='cancelled', updated_at=? "
                    "WHERE todo_id=? AND status='pending'",
                    (now, todo_id),
                )
            elif next_status == "open" and existing[0] == "completed":
                assignments.append("completed_at = NULL")

            if assignments:
                assignments.extend(["updated_at = ?", "version = version + 1"])
                values.extend([now, todo_id])
                connection.execute(
                    f"UPDATE todos SET {', '.join(assignments)} WHERE todo_id = ?",  # noqa: S608
                    values,
                )

        result = self.get_todo(todo_id)
        if result is None:  # pragma: no cover
            raise RuntimeError("todo disappeared after update")
        return result

    def delete_todo(self, todo_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM todos WHERE todo_id = ?", (todo_id,))
        return cursor.rowcount == 1

    def create_reminder(
        self,
        reminder_id: str,
        todo_id: str,
        reminder: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            todo = connection.execute(
                "SELECT status FROM todos WHERE todo_id = ?",
                (todo_id,),
            ).fetchone()
            if todo is None:
                raise KeyError(f"unknown todo: {todo_id}")
            if todo[0] == "completed":
                raise ValueError("cannot add a reminder to a completed todo")
            connection.execute(
                "INSERT INTO reminders "
                "(reminder_id, todo_id, scheduled_at, channel, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (
                    reminder_id,
                    todo_id,
                    reminder["scheduled_at"],
                    reminder.get("channel", "push"),
                    now,
                    now,
                ),
            )
        result = self.get_reminder(reminder_id)
        if result is None:  # pragma: no cover
            raise RuntimeError("reminder was not persisted")
        return result

    def list_reminders(self, todo_id: str) -> list[dict[str, Any]]:
        if self.get_todo(todo_id) is None:
            raise KeyError(f"unknown todo: {todo_id}")
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM reminders WHERE todo_id = ? ORDER BY scheduled_at, created_at",
                (todo_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_reminder(self, reminder_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM reminders WHERE reminder_id = ?",
                (reminder_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_reminder(self, reminder_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"scheduled_at", "channel", "status"}
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"unsupported reminder fields: {', '.join(sorted(invalid))}")
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT reminders.reminder_id, todos.status "
                "FROM reminders JOIN todos ON todos.todo_id = reminders.todo_id "
                "WHERE reminders.reminder_id = ?",
                (reminder_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"unknown reminder: {reminder_id}")
            if changes.get("status") == "pending" and existing[1] == "completed":
                raise ValueError("cannot reactivate a reminder for a completed todo")
            if changes:
                assignments = [f"{field} = ?" for field in changes]
                values = [*changes.values(), now, reminder_id]
                connection.execute(
                    f"UPDATE reminders SET {', '.join(assignments)}, updated_at = ? "  # noqa: S608
                    "WHERE reminder_id = ?",
                    values,
                )
        result = self.get_reminder(reminder_id)
        if result is None:  # pragma: no cover
            raise RuntimeError("reminder disappeared after update")
        return result

    def delete_reminder(self, reminder_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM reminders WHERE reminder_id = ?",
                (reminder_id,),
            )
        return cursor.rowcount == 1

    def claim_due_reminders(
        self,
        *,
        now: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Atomically reserve due reminders and create durable delivery attempts."""

        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT reminders.*, todos.title, todos.notes, todos.priority, todos.project_id "
                "FROM reminders JOIN todos ON todos.todo_id = reminders.todo_id "
                "WHERE reminders.status = 'pending' AND todos.status = 'open' "
                "AND datetime(reminders.scheduled_at) <= datetime(?) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM reminder_deliveries "
                "  WHERE reminder_deliveries.reminder_id = reminders.reminder_id "
                "  AND reminder_deliveries.status = 'processing'"
                ") "
                "ORDER BY datetime(reminders.scheduled_at), reminders.created_at LIMIT ?",
                (now, limit),
            ).fetchall()

            deliveries: list[dict[str, Any]] = []
            for row in rows:
                reminder = dict(row)
                attempt = int(reminder["attempts"]) + 1
                delivery_id = str(uuid4())
                payload = {
                    "title": reminder["title"],
                    "notes": reminder["notes"],
                    "priority": reminder["priority"],
                    "project_id": reminder["project_id"],
                    "message": f"Reminder: {reminder['title']}",
                }
                connection.execute(
                    "INSERT INTO reminder_deliveries "
                    "(delivery_id, reminder_id, todo_id, channel, scheduled_at, status, attempt, "
                    "payload_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?)",
                    (
                        delivery_id,
                        reminder["reminder_id"],
                        reminder["todo_id"],
                        reminder["channel"],
                        reminder["scheduled_at"],
                        attempt,
                        self._json(payload),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE reminders SET attempts = ?, last_error = NULL, updated_at = ? "
                    "WHERE reminder_id = ?",
                    (attempt, now, reminder["reminder_id"]),
                )
                deliveries.append({
                    "delivery_id": delivery_id,
                    "reminder_id": reminder["reminder_id"],
                    "todo_id": reminder["todo_id"],
                    "channel": reminder["channel"],
                    "scheduled_at": reminder["scheduled_at"],
                    "status": "processing",
                    "attempt": attempt,
                    "payload": payload,
                    "error": None,
                    "delivered_at": None,
                    "acknowledged_at": None,
                    "created_at": now,
                    "updated_at": now,
                })
            return deliveries

    def finish_reminder_delivery(
        self,
        delivery_id: str,
        *,
        succeeded: bool,
        finished_at: str,
        error: str | None = None,
        retry_at: str | None = None,
    ) -> dict[str, Any]:
        """Finalize a claimed delivery and either close or reschedule its reminder."""

        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                "SELECT * FROM reminder_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if delivery is None:
                raise KeyError(f"unknown delivery: {delivery_id}")
            if delivery["status"] != "processing":
                raise ValueError(f"delivery is already {delivery['status']}")

            if succeeded:
                connection.execute(
                    "UPDATE reminder_deliveries SET status='sent', delivered_at=?, updated_at=? "
                    "WHERE delivery_id=?",
                    (finished_at, finished_at, delivery_id),
                )
                connection.execute(
                    "UPDATE reminders SET status='sent', last_error=NULL, updated_at=? "
                    "WHERE reminder_id=? AND status='pending'",
                    (finished_at, delivery["reminder_id"]),
                )
            else:
                message = error or "delivery_failed"
                connection.execute(
                    "UPDATE reminder_deliveries SET status='failed', error=?, updated_at=? "
                    "WHERE delivery_id=?",
                    (message, finished_at, delivery_id),
                )
                if retry_at is not None:
                    connection.execute(
                        "UPDATE reminders SET status='pending', scheduled_at=?, last_error=?, updated_at=? "
                        "WHERE reminder_id=? AND status='pending'",
                        (retry_at, message, finished_at, delivery["reminder_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE reminders SET status='failed', last_error=?, updated_at=? "
                        "WHERE reminder_id=? AND status='pending'",
                        (message, finished_at, delivery["reminder_id"]),
                    )

        result = self.get_reminder_delivery(delivery_id)
        if result is None:  # pragma: no cover
            raise RuntimeError("delivery disappeared after completion")
        return result

    def recover_stale_reminder_deliveries(
        self,
        *,
        stale_before: str,
        retry_at: str,
        max_attempts: int,
    ) -> int:
        """Fail interrupted claims and make eligible reminders available again."""

        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT reminder_deliveries.delivery_id, reminder_deliveries.reminder_id, "
                "reminders.attempts FROM reminder_deliveries "
                "JOIN reminders ON reminders.reminder_id = reminder_deliveries.reminder_id "
                "WHERE reminder_deliveries.status='processing' "
                "AND datetime(reminder_deliveries.updated_at) <= datetime(?)",
                (stale_before,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE reminder_deliveries SET status='failed', error='worker_interrupted', "
                    "updated_at=? WHERE delivery_id=?",
                    (retry_at, row["delivery_id"]),
                )
                if int(row["attempts"]) >= max_attempts:
                    connection.execute(
                        "UPDATE reminders SET status='failed', last_error='worker_interrupted', "
                        "updated_at=? WHERE reminder_id=? AND status='pending'",
                        (retry_at, row["reminder_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE reminders SET scheduled_at=?, last_error='worker_interrupted', "
                        "updated_at=? WHERE reminder_id=? AND status='pending'",
                        (retry_at, retry_at, row["reminder_id"]),
                    )
        return len(rows)

    def get_reminder_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM reminder_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return self._delivery_record(row) if row is not None else None

    def list_reminder_deliveries(
        self,
        *,
        status: str = "all",
        channel: str = "all",
        unacknowledged_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if status != "all":
            conditions.append("status = ?")
            parameters.append(status)
        if channel != "all":
            conditions.append("channel = ?")
            parameters.append(channel)
        if unacknowledged_only:
            conditions.append("acknowledged_at IS NULL")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(limit)
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM reminder_deliveries {where} "  # noqa: S608
                "ORDER BY created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._delivery_record(row) for row in rows]

    def acknowledge_reminder_delivery(
        self,
        delivery_id: str,
        *,
        acknowledged_at: str,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reminder_deliveries SET acknowledged_at=COALESCE(acknowledged_at, ?), "
                "updated_at=? WHERE delivery_id=? AND status='sent'",
                (acknowledged_at, acknowledged_at, delivery_id),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    "SELECT status FROM reminder_deliveries WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if existing is None:
                    raise KeyError(f"unknown delivery: {delivery_id}")
                raise ValueError("only sent deliveries can be acknowledged")
        result = self.get_reminder_delivery(delivery_id)
        if result is None:  # pragma: no cover
            raise RuntimeError("delivery disappeared after acknowledgement")
        return result

    def append_task_event(self, task_id: str, event: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as connection:
            self._insert_event(connection, task_id, event, payload, utc_now().isoformat())

    def list_task_events(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, status, payload_json, created_at FROM task_events "
                "WHERE task_id=? ORDER BY event_id", (task_id,),
            ).fetchall()
        return [dict(event_id=row[0], event=row[1], payload=json.loads(row[2]), created_at=row[3])
                for row in rows]

    def save_workflow(self, workflow_id: str, definition: dict[str, Any]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO workflow_definitions (workflow_id, definition_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(workflow_id) DO UPDATE SET definition_json=excluded.definition_json, "
                "updated_at=excluded.updated_at",
                (workflow_id, self._json(definition), utc_now().isoformat()),
            )

    def list_workflows(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT workflow_id, definition_json, updated_at FROM workflow_definitions ORDER BY workflow_id"
            ).fetchall()
        return [dict(workflow_id=row[0], **json.loads(row[1]), updated_at=row[2]) for row in rows]

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT definition_json FROM workflow_definitions WHERE workflow_id=?", (workflow_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._lock, self._connect() as connection:
            return connection.execute(
                "DELETE FROM workflow_definitions WHERE workflow_id=?", (workflow_id,),
            ).rowcount == 1

    def recover_interrupted_tasks(self) -> int:
        """Mark interrupted tasks as failed after a process restart."""

        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks WHERE status IN ('queued', 'planning', 'delegated', 'running')"
            ).fetchall()
            for (task_id,) in rows:
                connection.execute(
                    "UPDATE tasks SET status='failed', error='process_interrupted', updated_at=? "
                    "WHERE task_id=?",
                    (now, task_id),
                )
                self._insert_event(
                    connection,
                    task_id,
                    "failed",
                    {"error": "process_interrupted", "recoverable": True},
                    now,
                )
        return len(rows)

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_definitions (
                    workflow_id TEXT PRIMARY KEY,
                    definition_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    task_type TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    plan_json TEXT,
                    result_json TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_project_status
                    ON tasks(project_id, status);
                CREATE INDEX IF NOT EXISTS idx_task_events_task
                    ON task_events(task_id, event_id);

                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS todos (
                    todo_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'completed')),
                    priority TEXT NOT NULL DEFAULT 'normal'
                        CHECK(priority IN ('low', 'normal', 'high', 'critical')),
                    due_at TEXT,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    recurrence_rule TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    reminder_id TEXT PRIMARY KEY,
                    todo_id TEXT NOT NULL REFERENCES todos(todo_id) ON DELETE CASCADE,
                    scheduled_at TEXT NOT NULL,
                    channel TEXT NOT NULL CHECK(channel IN ('dashboard', 'push', 'voice')),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'sent', 'failed', 'cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminder_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    reminder_id TEXT NOT NULL,
                    todo_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('processing', 'sent', 'failed')),
                    attempt INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    delivered_at TEXT,
                    acknowledged_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(reminder_id, attempt)
                );

                CREATE INDEX IF NOT EXISTS idx_todos_status_due
                    ON todos(status, due_at);
                CREATE INDEX IF NOT EXISTS idx_todos_project
                    ON todos(project_id, status);
                CREATE INDEX IF NOT EXISTS idx_reminders_due
                    ON reminders(status, scheduled_at);
                CREATE INDEX IF NOT EXISTS idx_reminders_todo
                    ON reminders(todo_id, scheduled_at);
                CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_status
                    ON reminder_deliveries(status, acknowledged_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_reminder
                    ON reminder_deliveries(reminder_id, attempt);
                """
            )
            connection.execute("PRAGMA optimize")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        task_id: str,
        status: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO task_events (task_id, status, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (task_id, status, TaskStore._json(payload), created_at),
        )

    @staticmethod
    def _json(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _delivery_record(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result
