"""Inspectable starting points; templates do not execute until explicitly requested."""
from app.harness.contracts import WorkflowDefinition


CODE_HEALTH = WorkflowDefinition.model_validate({
    "name": "Code health",
    "description": "Inspect Git status and Python syntax, then run unit tests after syntax passes.",
    "plan": {
        "max_parallel": 2,
        "steps": [
            {"step_id": "status", "tool": "git.status"},
            {"step_id": "syntax", "tool": "python.compile", "arguments": {"targets": ["app"]}},
            {"step_id": "tests", "tool": "python.test", "arguments": {"targets": ["tests"]},
             "depends_on": ["syntax"], "timeout_seconds": 120},
        ],
    },
})
