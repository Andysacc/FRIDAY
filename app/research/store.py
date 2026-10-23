from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.orchestrator.contracts import utc_now


class ResearchStore:
    """SQLite-backed queue and artifact catalogue shared by API and worker."""

    def __init__(self, database_path: Path, *, recover_interrupted: bool = False) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()
        if recover_interrupted:
            self.recover_interrupted_jobs()

    def create_job(
        self,
        *,
        topic: str,
        tier: str,
        estimated_seconds: int,
        source_target: int,
        deep_analysis_target: int,
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        job_id = str(uuid4())
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO research_jobs "
                "(job_id, topic, tier, status, phase, progress, estimated_seconds, source_target, "
                "deep_analysis_target, requested_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', 'Queued', 0, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    topic,
                    tier,
                    estimated_seconds,
                    source_target,
                    deep_analysis_target,
                    now,
                    now,
                ),
            )
            self._event(connection, job_id, "queued", "Research request accepted", 0, now)
        result = self.get_job(job_id)
        if result is None:  # pragma: no cover
            raise RuntimeError("research job disappeared after creation")
        return result

    def claim_next(self) -> dict[str, Any] | None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE status='queued' "
                "ORDER BY requested_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE research_jobs SET status='running', phase='Starting research', progress=1, "
                "started_at=COALESCE(started_at, ?), updated_at=? "
                "WHERE job_id=? AND status='queued'",
                (now, now, row["job_id"]),
            )
            self._event(connection, row["job_id"], "running", "Research worker claimed job", 1, now)
        return self.get_job(row["job_id"])

    def update_progress(
        self,
        job_id: str,
        *,
        phase: str,
        progress: int,
        sources_found: int | None = None,
        sources_analyzed: int | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        assignments = ["phase=?", "progress=?", "updated_at=?"]
        values: list[Any] = [phase, max(1, min(99, progress)), now]
        if sources_found is not None:
            assignments.append("sources_found=?")
            values.append(max(0, sources_found))
        if sources_analyzed is not None:
            assignments.append("sources_analyzed=?")
            values.append(max(0, sources_analyzed))
        values.append(job_id)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE research_jobs SET {', '.join(assignments)} "  # noqa: S608
                "WHERE job_id=? AND status='running'",
                values,
            )
            if cursor.rowcount != 1:
                raise ValueError("research job is no longer running")
            self._event(connection, job_id, "running", detail or phase, progress, now)
        return self._required_job(job_id)

    def finish_job(
        self,
        job_id: str,
        *,
        artifact_name: str,
        artifact_path: str,
        artifact_media_type: str,
        content_markdown: str,
        knowledge_key: str | None,
        sources_found: int,
        sources_analyzed: int,
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE research_jobs SET status='completed', phase='Complete', progress=100, "
                "artifact_name=?, artifact_path=?, artifact_media_type=?, content_markdown=?, "
                "knowledge_key=?, sources_found=?, sources_analyzed=?, finished_at=?, updated_at=? "
                "WHERE job_id=? AND status='running'",
                (
                    artifact_name,
                    artifact_path,
                    artifact_media_type,
                    content_markdown,
                    knowledge_key,
                    sources_found,
                    sources_analyzed,
                    now,
                    now,
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("research job is no longer running")
            self._event(connection, job_id, "completed", "Research artifact created", 100, now)
        return self._required_job(job_id)

    def fail_job(self, job_id: str, error: str) -> dict[str, Any]:
        now = utc_now().isoformat()
        message = error.strip()[:2000] or "research_failed"
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE research_jobs SET status='failed', phase='Failed', error=?, "
                "finished_at=?, updated_at=? WHERE job_id=? AND status IN ('queued','running')",
                (message, now, now, job_id),
            )
            if cursor.rowcount == 1:
                self._event(connection, job_id, "failed", message, 100, now)
        return self._required_job(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE research_jobs SET status='cancelled', phase='Cancelled', finished_at=?, updated_at=? "
                "WHERE job_id=? AND status IN ('queued','running')",
                (now, now, job_id),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    "SELECT status FROM research_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if existing is None:
                    raise KeyError(f"unknown research job: {job_id}")
                raise ValueError(f"research job is already {existing[0]}")
            self._event(connection, job_id, "cancelled", "Cancelled by user", 100, now)
        return self._required_job(job_id)

    def retry_job(self, job_id: str) -> dict[str, Any]:
        """Return a failed job to the durable queue without creating a duplicate."""

        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE research_jobs SET status='queued', phase='Queued for retry', progress=0, "
                "sources_found=0, sources_analyzed=0, error=NULL, requested_at=?, started_at=NULL, "
                "finished_at=NULL, updated_at=? WHERE job_id=? AND status='failed'",
                (now, now, job_id),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    "SELECT status FROM research_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if existing is None:
                    raise KeyError(f"unknown research job: {job_id}")
                raise ValueError(f"research job is {existing[0]}, not failed")
            self._event(connection, job_id, "queued", "Queued for retry", 0, now)
        return self._required_job(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM research_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return row is not None and row[0] == "cancelled"

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._job_record(row) if row is not None else None

    def get_artifact(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT job_id, topic, tier, status, artifact_name, artifact_path, "
                "artifact_media_type, content_markdown, knowledge_key, updated_at "
                "FROM research_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_knowledge_artifacts(self, knowledge_keys: list[str]) -> list[dict[str, Any]]:
        if not knowledge_keys:
            return []
        placeholders = ",".join("?" for _ in knowledge_keys)
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT job_id, topic, knowledge_key, artifact_name, artifact_path, "  # noqa: S608
                f"content_markdown FROM research_jobs WHERE status='completed' "
                f"AND tier='scholar' AND knowledge_key IN ({placeholders})",
                knowledge_keys,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM research_jobs ORDER BY requested_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._job_record(row) for row in rows]

    def list_knowledge(self, *, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        pattern = f"%{query.strip()}%"
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT job_id, topic, knowledge_key, artifact_name, updated_at "
                "FROM research_jobs WHERE status='completed' AND tier='scholar' "
                "AND knowledge_key IS NOT NULL AND (topic LIKE ? OR knowledge_key LIKE ?) "
                "ORDER BY updated_at DESC LIMIT ?",
                (pattern, pattern, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_sources(self, job_id: str, sources: list[dict[str, Any]]) -> None:
        if not sources:
            return
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO research_sources "
                "(job_id, source_key, provider, title, url, doi, authors_json, published_at, "
                "abstract, citation_count, relevance_score, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        job_id,
                        str(source.get("source_key") or source.get("doi") or source.get("url")),
                        source.get("provider", "unknown"),
                        source.get("title", "Untitled"),
                        source.get("url"),
                        source.get("doi"),
                        json.dumps(source.get("authors", []), ensure_ascii=False),
                        source.get("published_at"),
                        source.get("abstract"),
                        int(source.get("citation_count") or 0),
                        float(source.get("score") or 0),
                        now,
                    )
                    for source in sources
                ],
            )

    def list_sources(self, job_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM research_sources WHERE job_id=? "
                "ORDER BY relevance_score DESC, citation_count DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["authors"] = json.loads(item.pop("authors_json"))
            results.append(item)
        return results

    def recover_interrupted_jobs(self) -> int:
        """Put interrupted research back in the durable queue after a restart."""

        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM research_jobs WHERE status='running'"
            ).fetchall()
            for (job_id,) in rows:
                connection.execute(
                    "UPDATE research_jobs SET status='queued', phase='Recovered after restart', "
                    "progress=MAX(1, progress), updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
                self._event(connection, job_id, "queued", "Recovered after worker restart", 1, now)
        return len(rows)

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS research_jobs (
                    job_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    tier TEXT NOT NULL CHECK(tier IN ('summary','fun_fact','scholar')),
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
                    phase TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
                    estimated_seconds INTEGER NOT NULL,
                    source_target INTEGER NOT NULL,
                    deep_analysis_target INTEGER NOT NULL,
                    sources_found INTEGER NOT NULL DEFAULT 0,
                    sources_analyzed INTEGER NOT NULL DEFAULT 0,
                    artifact_name TEXT,
                    artifact_path TEXT,
                    artifact_media_type TEXT,
                    content_markdown TEXT,
                    knowledge_key TEXT UNIQUE,
                    error TEXT,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS research_sources (
                    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE CASCADE,
                    source_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    doi TEXT,
                    authors_json TEXT NOT NULL DEFAULT '[]',
                    published_at TEXT,
                    abstract TEXT,
                    citation_count INTEGER NOT NULL DEFAULT 0,
                    relevance_score REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, source_key)
                );

                CREATE TABLE IF NOT EXISTS research_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_research_jobs_status_requested
                    ON research_jobs(status, requested_at);
                CREATE INDEX IF NOT EXISTS idx_research_jobs_tier_updated
                    ON research_jobs(tier, updated_at);
                CREATE INDEX IF NOT EXISTS idx_research_sources_job_rank
                    ON research_sources(job_id, relevance_score DESC, citation_count DESC);
                CREATE INDEX IF NOT EXISTS idx_research_events_job
                    ON research_events(job_id, event_id);
                """
            )
            connection.execute("PRAGMA optimize")

    def _required_job(self, job_id: str) -> dict[str, Any]:
        result = self.get_job(job_id)
        if result is None:
            raise KeyError(f"unknown research job: {job_id}")
        return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        job_id: str,
        status: str,
        detail: str,
        progress: int,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO research_events (job_id, status, detail, progress, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, status, detail, max(0, min(100, progress)), created_at),
        )

    @staticmethod
    def _job_record(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.pop("artifact_path", None)
        item.pop("content_markdown", None)
        return item
