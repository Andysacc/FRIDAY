# FRIDAY Orchestration Architecture

Coding tasks use the execution pipeline below. The [multi-tool harness](HARNESS.md)
supports dependency graphs over registered tools, reusable definitions, and
per-step durable events.

## Coding Task Execution

Qwen coordinates each coding task through this pipeline:

```text
HTTP coding task
      |
      v
QwenOrchestrator ----> SQLite task/event state
      |
      | AgentTaskMessage (JSON v1.0)
      v
CodingAgent
      |
      | ExecutionRequest (JSON v1.0)
      v
ExecutionService
      |
      | ExecutionResult (JSON v1.0)
      v
CodingAgent ----> Qwen merge ----> OrchestrationResult (JSON v1.0)
```

Qwen plans and summarizes. It never opens a subprocess, imports user code, or
connects to a database. The coding agent is also unable to execute directly; it
can only call `ExecutionService.execute()` with a validated `ExecutionRequest`.

## Contracts

All internal messages are Pydantic models in
`app/orchestrator/contracts.py`. Every component boundary includes
`schema_version: "1.0"` and stable task/execution identifiers.

The public `CodingTaskRequest.execution` field is optional:

- Omitted: Qwen selects one operation from the allow-list and its JSON is
  schema-validated before delegation.
- Included: the caller pins the operation for reproducibility. Qwen still owns
  task state, delegation, and result merging.

Invalid or non-JSON model output fails closed. No operation is inferred from an
invalid response.

## Execution Allow-list

| Tool | Actions | Notes |
| --- | --- | --- |
| `python` | `test`, `compile` | `unittest` or `pytest`; relative targets only |
| `git` | `status`, `diff`, `log` | Read-only operations only |
| `docker` | `compose_config`, `compose_ps` | No raw Docker arguments |
| `db` | `sqlite_integrity`, `sqlite_schema` | SQLite connections use read-only mode |
| `workspace` | `scaffold` | Explicit plans only; fixed project directory and two fixed files |

The service does not accept shell text, Python snippets, SQL queries, absolute
paths, parent-directory traversal, or unknown arguments. Processes use
`create_subprocess_exec` with an argument vector and a bounded timeout/output
size. Tool availability still depends on the runtime image; a missing Git or
Docker executable becomes a structured failed result.

`workspace.scaffold` is the only mutation operation. Qwen cannot select it from
the general coding planner: it is constructed by the project workflow only
after the caller sets `scaffold=true`. The service derives
`projects/<project_id>` itself, writes only `README.md` and
`.friday-project.json`, and rejects an existing target rather than overwriting
it.

## State and Recovery

`TaskStore` persists projects, tasks, attempts, plans, results, and append-only
status events in `/data/orchestrator.sqlite3` by default. The first recovery
policy is intentionally conservative: tasks left in `planning`, `delegated`, or
`running` after process interruption are marked failed with
`process_interrupted`. A later increment can retry operations that are proven
idempotent.

State progression is:

```text
queued -> planning -> delegated -> running -> succeeded | failed
```

## Deliberate Limits of This Increment

- General dashboard chat and LiveKit voice conversation remain on their current
  routes; recognized project-test intents are routed into task orchestration.
- The execution boundary is an injectable in-process service, not yet an
  independently deployed privileged worker.
- General code-writing/refactor operations are not enabled. The only write is
  the separately authorized, fixed project scaffold.
- Quant experiments, reviewer approval, and generalized artifacts remain future
  layers. Multi-step DAGs now run through `app/harness` using the same task state.

Keeping these limits explicit prevents the first milestone from silently
granting Qwen arbitrary local execution or workspace mutation rights.

## Project Pipeline Integration

Projects are registered in the same SQLite database as orchestrator tasks. The
dashboard keeps a local responsive cache, while the backend stores the active
project and authoritative pipeline state used by chat and voice.

All testing entry points call `POST /api/projects/{project_id}/tests` with a
structured source, framework, and target list. The endpoint creates an explicit
`python.test` execution plan, delegates it through `QwenOrchestrator` and
`CodingAgent`, and returns the persisted task result.

```text
Projects button ─┐
Chat /test ──────┼─> project test endpoint ─> coding agent ─> execution service
Voice request ───┘                                      |
                                                        v
                         project Logs <─ task history <─ SQLite
```

The transition from Execution to Review is server-controlled and happens only
when the test task succeeds. Failure and transport errors leave the project in
Execution. The Logs tab reads project task history, so voice-triggered and
chat-triggered runs remain visible after the originating interaction ends.

## Project Creation Integration

`POST /api/orchestrator/projects` accepts an explicit objective. Qwen returns a
schema-validated `ProjectBlueprint` containing an ID, display name, scope,
first milestone, and observable success criteria. Validation completes before
the project registry changes. The project begins in Intake at zero progress and
can be activated in the same request.

```text
Explicit chat/API instruction
          |
          v
Qwen project blueprint -> schema validation -> project registry -> active project
                                                    |
                                  scaffold=true ----+
                                                    v
                                    fixed workspace.scaffold task
```

Scaffolding is opt-in because it mutates the workspace. A failed scaffold
returns `status=partial`: the durable project remains registered and the failed
task is retained for inspection. Project deletion does not delete its scaffold
directory; workspace files require a separate deliberate cleanup action.
