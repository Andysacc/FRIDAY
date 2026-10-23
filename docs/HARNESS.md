# FRIDAY multi-tool harness

FRIDAY adapts DeepSeek Harness's plugin registry, schema-driven capabilities,
separation of planning and execution, and durable call/result patterns into the
existing FastAPI/Qwen/SQLite stack. This is an independent Python implementation:
no upstream source is vendored, no Node service is required, and dsh/Cordis
plugins cannot be loaded directly.

Design references inspected on October 21, 2026:

- [DeepSeek Harness architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)
- [Guarded execution pipeline](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/tool-execution-pipeline.md)
- [Upstream repository](https://github.com/deepseek-ai/deepseek-harness), developer preview;
  inspected head `687f282e8625c6b8d11ff58211a90524be2e4317`.

## Execution structure

```text
/workflow or HTTP request
          |
          v
explicit plan | saved definition | Qwen + tool schemas
          |
          v
validate complete graph + registered tools + argument schemas + path policy
          |
          v
dependency scheduler (1–4 concurrent steps)
          |
          v
trusted tool plugin -> existing ExecutionService -> structured result
          |
          v
SQLite task + call/result events -> dashboard completions + project logs
```

`WorkflowPlan` supports 1–32 steps with unique IDs and explicit `depends_on`
edges. Qwen-generated plans are limited to 8 steps. Forward references work;
cycles, self-dependencies, missing dependencies, and unknown fields are rejected.
All tools and arguments are checked before any plugin executes. A registry
snapshot and persisted plan fix each run's capability set and graph.

Ready steps run as slots become available. A failure, rejection, or timeout
skips descendants; independent branches continue. Overall success requires every
step to succeed. Summaries derive from actual results and do not require another
model call. There are no implicit retries or continue-on-error edges.

## Public API

| Method | Endpoint | Behavior |
| --- | --- | --- |
| GET | `/api/harness/tools` | Names, descriptions, model visibility, JSON argument schemas |
| GET | `/api/harness/templates` | Inspectable built-in starting structures |
| GET | `/api/harness/workflows` | List saved reusable definitions |
| PUT | `/api/harness/workflows/{id}` | Validate and save/replace a definition |
| GET | `/api/harness/workflows/{id}` | Read a saved definition |
| DELETE | `/api/harness/workflows/{id}` | Remove a definition; historical runs remain |
| POST | `/api/harness/runs` | Execute inline, saved, or Qwen-planned graph |
| GET | `/api/orchestrator/tasks/{task_id}` | Durable plan, state, and final step results |
| GET | `/api/orchestrator/tasks/{task_id}/events` | Ordered task and tool call/result history |
| GET | `/api/orchestrator/completions` | Existing completion feed, including workflows |

Run requests accept `task_id`, `project_id`, `objective`, `plan`, `workflow_id`,
and `model`. Choose `plan` or `workflow_id`; omitting both requests Qwen planning.
Duplicate task IDs return HTTP 409 without executing again. Structural request
errors return 422. Execution/planning failures return a durable failed run with
HTTP 200 and `status=failed`, matching Friday's task result convention.

Save a reproducible structure:

```bash
curl -X PUT http://localhost:8080/api/harness/workflows/backend-check \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"Backend check",
    "description":"Syntax must pass before the unit tests start.",
    "plan":{
      "max_parallel":2,
      "steps":[
        {"step_id":"syntax","tool":"python.compile","arguments":{"targets":["app"]}},
        {"step_id":"tests","tool":"python.test","arguments":{"targets":["tests"]},"depends_on":["syntax"],"timeout_seconds":120}
      ]
    }
  }'

curl -X POST http://localhost:8080/api/harness/runs \
  -H 'Content-Type: application/json' \
  -d '{"workflow_id":"backend-check","project_id":"friday"}'
```

Saving or editing a definition does not execute it. A saved definition is
revalidated against the current registry on every run. Definitions live in the
additive `workflow_definitions` SQLite table; runs use existing tasks with
`task_type=workflow`. Project deletion removes its task history, while reusable
workflow definitions remain independent of project ownership.

## Dashboard commands

- `/workflow`: list tools, saved IDs, templates, and usage.
- `/workflow template code-health`: run the supplied graph. Git status and
  syntax run independently; tests wait for syntax.
- `/workflow run backend-check`: execute a saved structure for the active project.
- `/workflow Inspect Git status and validate Python syntax`: ask Qwen to compose
  and immediately execute a graph.

Workflow completions include every step's status and available console output.
The existing completion poll also covers API-triggered runs. Project Logs label
workflow tasks separately from test tasks. Voice currently retains its existing
single-operation test routing.

## Built-in capabilities

| Tool | Arguments (defaults omitted here where obvious) |
| --- | --- |
| `python.test` | `framework`: unittest/pytest; `targets`: one directory for unittest; `pattern` |
| `python.compile` | `targets`: relative Python files/directories |
| `git.status` | None |
| `git.diff` | `staged`: boolean; `paths`: relative paths |
| `git.log` | `limit`: 1–100 |
| `docker.compose_config` | `compose_file`: relative path |
| `docker.compose_ps` | `compose_file`: relative path |
| `db.sqlite_integrity` | `path`: existing workspace-relative SQLite file |
| `db.sqlite_schema` | `path`: existing workspace-relative SQLite file |

Each step has a 1–120 second timeout. Unknown arguments, absolute/traversing
paths, option-like paths, and symlinks escaping `EXECUTION_WORKSPACE` fail closed.
The deterministic executor rechecks paths at dispatch. SQLite remains read-only.
Python syntax checking creates bytecode; unit tests execute workspace code under
the existing execution policy. The harness does not add a process sandbox.

Tools require their underlying runtime: Git requires Git plus a checkout; Docker
requires its CLI and daemon access; pytest requires pytest. The current minimal
Docker image does not include Git or Docker. Its Python syntax/test workflow
works directly; the three-step `code-health` template reports a structured Git
failure there. SQLite paths stay within `EXECUTION_WORKSPACE`; in Compose the
separate `/data` volume is not implicitly exposed as a workspace tool path.

`workspace.scaffold` is deliberately absent from this registry. Friday's
existing explicit project scaffolding path remains the owner of that operation.
The model cannot authorize new tools, plugin imports, package installs, shell
text, source code, arbitrary SQL, or dynamic result-to-argument interpolation.

## Add a trusted tool

Implement a typed Python plugin and register it in `build_harness()` in
`app/main.py` after constructing the built-in registry. Registration is
application code, never an HTTP or model-supplied module path.

```python
from app.harness.contracts import Contract, ToolOutcome
from app.harness.registry import ToolPlugin

class CompareArguments(Contract):
    field: str = "value"

async def compare(arguments, context):
    values = {
        step_id: result.outcome.output.get(arguments.field)
        for step_id, result in context.dependencies.items()
    }
    return ToolOutcome(status="succeeded", output={"values": values})

registry.register(ToolPlugin(
    name="analysis.compare",
    description="Collect a field from declared dependency results.",
    arguments_model=CompareArguments,
    execute=compare,
    model_selectable=True,
))
```

`Contract` forbids unknown fields. A plugin may supply a synchronous
`validate_arguments` callback for policy checks during preflight and dispatch.
Plugin names are `namespace.action`; duplicates are rejected. `unregister()`
removes a capability for future registry snapshots. Custom tools are hidden from
Qwen unless explicitly marked `model_selectable=True`; explicit plans can select
registered hidden tools. This flag governs model planning, not caller access
control.

`ToolContext` supplies task, project, and step IDs, the timeout, and isolated
copies of only the declared dependencies. Consumer mutations cannot rewrite
recorded predecessor results. Return `ToolOutcome` with JSON-serializable
`output` and the truthful status; built-ins also return an `ExecutionResult`.
Results are snapshotted through JSON and capped at 150,000 characters per tool.
Exceptions become failed steps; timeouts cancel and await the coroutine. Plugins
must cooperate with cancellation and clean up resources they create.

The service persists `tool.call` before dispatch and `tool.result` after
settlement. Cancellation terminates and reaps existing executor subprocess
groups before marking the parent task failed. On process restart, interrupted
work fails with `process_interrupted`, preserving prior step events for audit.
Runs are not automatically resumed or replayed, avoiding duplicate side effects.

## Model budget and limits

`HARNESS_NUM_PREDICT=2048` and `HARNESS_NUM_CTX=8192` allow more JSON and tool
schema context than normal chat. The same default Qwen model remains coordinator;
requests can select an installed model through the existing `model` field.
Switching between harness and chat context sizes may cause Ollama to reload the
model context. Small models may still emit invalid plans, which fail without
executing any tool. Explicit plans and saved workflows do not contact Ollama.

This increment provides tool composition, not Cordis hot reload, an MCP client,
a recursive agent loop, remote execution, a background workflow worker, operator
approval queues, or durable automatic resume. Runs currently execute during the
HTTP request; task events can be inspected from another request while running.

Validation:

```bash
.venv/bin/python -m unittest discover -v
node --check app/static/app.js
```
