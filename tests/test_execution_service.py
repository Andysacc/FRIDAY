import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.execution.service import ExecutionService
from app.orchestrator.contracts import ExecutionRequest


class ExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_directory.name)
        self.service = ExecutionService(self.workspace)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    async def test_python_compile_succeeds(self) -> None:
        package = self.workspace / "sample"
        package.mkdir()
        (package / "valid.py").write_text("answer = 42\n")
        request = ExecutionRequest(
            task_id="task-1",
            tool="python",
            action="compile",
            arguments={"targets": ["sample"]},
        )

        result = await self.service.execute(request)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("shell", result.metadata)

    async def test_path_traversal_is_rejected_before_execution(self) -> None:
        request = ExecutionRequest(
            task_id="task-2",
            tool="python",
            action="compile",
            arguments={"targets": ["../outside"]},
        )

        result = await self.service.execute(request)

        self.assertEqual(result.status, "rejected")
        self.assertIn("escapes", result.error or "")
        self.assertIsNone(result.exit_code)

    async def test_unknown_arguments_are_rejected(self) -> None:
        request = ExecutionRequest(
            task_id="task-3",
            tool="git",
            action="status",
            arguments={"command": "anything"},
        )

        result = await self.service.execute(request)

        self.assertEqual(result.status, "rejected")
        self.assertIn("unsupported arguments", result.error or "")

    async def test_sqlite_schema_is_read_only_and_structured(self) -> None:
        data_directory = self.workspace / "data"
        data_directory.mkdir()
        database_path = data_directory / "sample.sqlite3"
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")

        request = ExecutionRequest(
            task_id="task-4",
            tool="db",
            action="sqlite_schema",
            arguments={"path": "data/sample.sqlite3"},
        )

        result = await self.service.execute(request)

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.metadata["read_only"])
        self.assertIn('"name": "runs"', result.stdout)

    async def test_workspace_scaffold_writes_only_fixed_starter_files(self) -> None:
        request = ExecutionRequest(
            task_id="task-5",
            tool="workspace",
            action="scaffold",
            arguments={
                "project_id": "research-lab",
                "name": "Research Lab",
                "description": "A constrained research workspace.",
                "milestone": "Validate the first experiment",
                "objective": "Build a reproducible research pipeline",
                "success_criteria": ["One experiment runs", "Results are documented"],
            },
        )

        result = await self.service.execute(request)

        self.assertEqual(result.status, "succeeded")
        project_directory = self.workspace / "projects" / "research-lab"
        self.assertEqual(
            sorted(path.name for path in project_directory.iterdir()),
            [".friday-project.json", "README.md"],
        )
        self.assertIn("One experiment runs", (project_directory / "README.md").read_text())
        self.assertEqual(result.metadata["directory"], "projects/research-lab")

    async def test_workspace_scaffold_never_overwrites_existing_directory(self) -> None:
        project_directory = self.workspace / "projects" / "existing"
        project_directory.mkdir(parents=True)
        protected = project_directory / "README.md"
        protected.write_text("keep me\n")
        request = ExecutionRequest(
            task_id="task-6",
            tool="workspace",
            action="scaffold",
            arguments={
                "project_id": "existing",
                "name": "Existing",
                "description": "Existing project.",
                "milestone": "Do not overwrite",
                "objective": "Preserve existing content",
                "success_criteria": ["Existing files remain unchanged"],
            },
        )

        result = await self.service.execute(request)

        self.assertEqual(result.status, "rejected")
        self.assertIn("already exists", result.error or "")
        self.assertEqual(protected.read_text(), "keep me\n")


if __name__ == "__main__":
    unittest.main()
