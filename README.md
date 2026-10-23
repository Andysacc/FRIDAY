# FRIDAY

FRIDAY is a local-first AI command center with streaming chat, a reactive LiveKit voice link, live system telemetry, diagnostics, task and automation controls, Docker packaging, and local model support through Ollama.

Qwen coordinates durable coding tasks and delegates validated operations to a
deterministic execution service.

## Command Center

The dashboard includes:

- Streaming local chat with per-session model selection
- In-memory file reading for text, source code, CSV, JSON, PDF, and DOCX attachments
- Voice link controls and connection activity logs
- Live CPU, memory, disk, audio, and network telemetry
- API, Ollama, LiveKit, and dashboard-tool diagnostics
- Persistent local todos, durable multi-project pipelines and test logs, automation switches, and preferences
- Automatic chat completion reports from the orchestrator, including test status, summary, duration, exit code, and console output
- A read-only project file index and safe dashboard terminal
- A searchable command palette (`Command/Ctrl + K`) and responsive mobile navigation
- Three-tier local research with durable progress, cited DOCX/Markdown reports, and `@` knowledge recall

## Quick Start

1. Create your environment file:

   ```bash
   cp .env.example .env
   ```

2. On macOS, start the native Ollama app so Qwen can use Apple Metal:

   ```bash
   open -a Ollama
   ```

3. Start FRIDAY:

   ```bash
   docker compose up --build
   ```

4. Open [http://localhost:8080](http://localhost:8080).

FRIDAY waits for Ollama to pull the configured model (`qwen2.5:0.5b` by default)
before the dashboard starts. Native Ollama stores the models locally in
`~/.ollama`; no cloud inference account is required.

## Local Model

FRIDAY uses the Ollama service at `OLLAMA_BASE_URL`. The default macOS setting is
`http://host.docker.internal:11434`, which lets the Dockerized dashboard use the
native, Metal-accelerated Ollama app. A pinned CPU-only Ollama container remains
available as a fallback on host port `11435`; set `OLLAMA_BASE_URL=http://ollama:11434`
to use that fallback from the Compose services.

Set the fixed chat model with:

```env
DEFAULT_MODEL=qwen2.5:0.5b
```

Terminal chat and voice share the same context and keep-alive settings so
switching modes does not reload the model with a different context size.
Voice uses a smaller output cap to keep CPU response times bounded:

```env
OLLAMA_NUM_PREDICT=128
OLLAMA_VOICE_NUM_PREDICT=32
OLLAMA_NUM_CTX=2048
OLLAMA_KEEP_ALIVE=5m
```

Leave `OLLAMA_NUM_THREAD` unset to let Ollama select the thread count.

Voice streams Ollama text into short phrases (up to seven words before a word
boundary), synthesizing each phrase without waiting for the full reply. Piper
emits audio as each synthesis chunk becomes available. Call logs include
`first text ready` and `first audio ready` timings for diagnosis; these measure
individual pipeline stages, not microphone-to-speaker latency.

The low-latency defaults use `WHISPER_BEAM_SIZE=1`, a 0.35-second VAD silence
window, and endpoint delays of 0.3–1.5 seconds. Whisper reuses LiveKit's VAD
segmentation instead of running a second VAD pass. If Friday cuts in during
natural pauses, increase `VAD_MIN_SILENCE_DURATION` and
`VOICE_MIN_ENDPOINTING_DELAY` in `.env`. Short phrase synthesis may introduce
slight pauses within a reply. Rebuild/recreate `friday-agent` after changing
voice code or environment settings.

Heart is the selected output voice, using the local Kokoro ONNX engine
(`TTS_PROVIDER=kokoro`, `KOKORO_VOICE=af_heart`). See
[voice auditions and setup](docs/voice-auditions.md) for the eleven American
female voice samples, provider options, and rollback to Piper. An optional
output-only Voicebox server adapter is also available.


## Chat Commands

In the dashboard, use `/model` to show the active model and `/models`
to list the Ollama models available in the local Ollama store. Select one for
the current browser session with `/model <name>`; for example,
`/model qwen2.5:0.5b`.

Commands are handled locally and are never sent to the model:

- `/help` — list available commands
- `/clear` — clear the conversation
- `/call` and `/bye` — start and stop voice mode
- `/project <objective>` — let Qwen plan, register, and activate a new Intake project
- `/project --scaffold <objective>` — also authorize FRIDAY's fixed two-file starter workspace
- `/workflow` — inspect registered tools, saved workflows, and templates
- `/workflow template code-health` — run the supplied syntax/test/Git graph
- `/workflow run <saved-id>` — run a reusable workflow
- `/workflow <objective>` — ask Qwen to compose and execute a validated graph

## File Reading

Select the paperclip beside the message box to attach up to three files, or use
**Tools → Files** to attach a file from FRIDAY's read-only project index. Friday
extracts readable text from text/source files, CSV, JSON, PDF, and DOCX files and
adds it to the next chat request. Browser uploads are processed in memory and
are not stored on the server. Each file is limited to 10 MB and extracted text
is shortened to 8,000 characters so local-model context remains bounded.

## Local Research

Research uses a larger specialist model alongside the chat coordinator:

```env
DEFAULT_MODEL=qwen2.5:0.5b
RESEARCH_MODEL=qwen3:8b
RESEARCH_EMBEDDING_MODEL=bge-m3
```

Set `RESEARCH_MODEL=qwen3:14b` when the host has enough memory. Docker Compose
pulls both language models and the embedding model, then runs a durable research
worker alongside self-hosted SearXNG and Qdrant services.

On Apple Silicon, keep the 8B specialist on native Ollama. Running it inside a
Linux container disables Metal and makes long Scholar synthesis impractically
slow. The default 8K research context is designed for an 8 GB runtime allocation;
the worker uses bounded hierarchical synthesis instead of placing the full corpus
in one prompt.

Research can be started from the **Research** module or chat:

```text
Friday do a summary research on quantum computing
Friday do a fun fact research on Costa Rica
Friday do a scholar research on particle physics
```

Summary and Fun Fact jobs start after estimation and produce DOCX reports.
Scholar jobs always show the estimated duration and corpus scope first; reply
`start research` to confirm. Scholar reports are saved as Markdown and become
available to later chat through the `@` autocomplete.

Artifacts and source manifests are organized under:

```text
data/Research/
  Summary/
  Fun Fact/
  Scholar/
```

Scholar discovery cross-references OpenAlex, Crossref, Semantic Scholar, and
arXiv. Optional free API keys can be added to `.env` to improve allowance and
reliability. Google remains one SearXNG discovery engine with automatic
fallbacks because upstream automated searches can be rate-limited.

## Project Shape

```text
app/
  harness/                Typed tool plugins, reusable workflows, and dependency scheduling
  agents/coding.py        Coding task agent
  execution/service.py    Allow-listed Python, Git, Docker, and SQLite runner
  orchestrator/           Qwen planning, contracts, delegation, and merge logic
  state/task_store.py     Durable SQLite task state and recovery events
  main.py                 FastAPI routes
  providers/              Ollama adapter
  static/                 Glass chat and voice interface
docs/ORCHESTRATION.md     Contracts and execution boundaries
tests/                    Deterministic service and orchestration tests
docker-compose.yml       FRIDAY plus Ollama
Dockerfile               App image
.env.example             Configuration template
```

## Coding Task Orchestration

Submit a natural-language coding objective and let Qwen choose one safe
operation:

```bash
curl -X POST http://localhost:8080/api/orchestrator/tasks/coding \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"friday","objective":"Run the backend unit tests"}'
```

For reproducible automation, provide the exact structured execution plan and
Qwen will delegate and merge the result without choosing the operation:

```bash
curl -X POST http://localhost:8080/api/orchestrator/tasks/coding \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id":"friday",
    "objective":"Run the backend unit tests",
    "execution": {
      "tool":"python",
      "action":"test",
      "arguments":{"framework":"unittest","targets":["tests"]},
      "timeout_seconds":30
    }
  }'
```

The response includes the plan, specialist-agent result, execution output, and
Qwen's merged summary. Retrieve the persisted record later with
`GET /api/orchestrator/tasks/{task_id}`.

## Multi-tool Harness

FRIDAY adapts the plugin composition and guarded tool execution patterns from
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) into its
existing Python infrastructure. Qwen remains the coordinator; the Node/Cordis
runtime is not installed or required. This is a native adaptation, not a dsh
plugin or SDK compatibility layer.

The harness provides nine typed operations over Python, Git, Docker Compose,
and SQLite, reusable SQLite-backed workflow definitions, and dependency graphs
with up to four concurrent steps. Entire plans are validated before execution;
failed dependencies skip their descendants while independent branches finish.
Task results and per-step events use Friday's existing durable task store,
completion feed, and project logs.

Run a deterministic graph without contacting Ollama:

```bash
curl -X POST http://localhost:8080/api/harness/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id":"friday",
    "objective":"Validate Python syntax before running tests",
    "plan":{"steps":[
      {"step_id":"syntax","tool":"python.compile","arguments":{"targets":["app"]}},
      {"step_id":"tests","tool":"python.test","arguments":{"targets":["tests"]},"depends_on":["syntax"],"timeout_seconds":120}
    ]}
  }'
```

Omit `plan` to let Qwen compose one, or pass `workflow_id` to load a saved
definition. Workflow planning uses `HARNESS_NUM_PREDICT=2048` and
`HARNESS_NUM_CTX=8192`, independently of chat's smaller output/context budgets.
Invalid model output fails closed; explicit plans work without a model.

See [the harness guide](docs/HARNESS.md) for saving structures, adding trusted
plugins, API examples, runtime requirements, and current limits.

## Durable Todos and Reminders

Todos are stored in Friday's SQLite database rather than browser storage. The
dashboard uses the same API that future PWA and voice clients will use:

- `POST /api/todos` and `GET /api/todos?status=open|completed|all`
- `GET`, `PATCH`, and `DELETE /api/todos/{todo_id}`
- `POST` and `GET /api/todos/{todo_id}/reminders`
- `PATCH` and `DELETE /api/reminders/{reminder_id}`

Due and reminder timestamps must include a timezone offset and are normalized
to UTC. Completing a todo cancels its pending reminders atomically.

The `friday-reminders` Compose service polls the same SQLite database, claims
due reminders transactionally, and records every attempt in
`reminder_deliveries`. Dashboard reminders are made available through
`GET /api/reminder-deliveries` and acknowledged with
`POST /api/reminder-deliveries/{delivery_id}/acknowledge`. Interrupted claims
are recovered, and retryable driver failures use exponential backoff. Configure
the worker with `REMINDER_POLL_SECONDS`, `REMINDER_BATCH_SIZE`,
`REMINDER_MAX_ATTEMPTS`, `REMINDER_RETRY_SECONDS`, and
`REMINDER_CLAIM_TIMEOUT_SECONDS`.

The dashboard task form can schedule a dashboard reminder directly. Push and
voice channels remain valid API choices for future transports, but the worker
marks them failed with a clear `*_channel_not_configured` delivery error until
those drivers are installed.

## Progressive Web App

FRIDAY includes a web app manifest, install icons, and a service worker, so the
dashboard can be installed from a supported desktop or mobile browser. Open
**Settings → Friday app** and select **Install** when the browser offers it. On
iPhone or iPad, open FRIDAY in Safari and use **Share → Add to Home Screen**.

Installation and service workers require HTTPS, except that browsers allow
`http://localhost` during local development. When opening FRIDAY from another
device, expose it through an HTTPS endpoint such as a Tailscale HTTPS URL or a
reverse proxy with TLS. The service worker caches only the application shell;
todo, reminder, project, and other `/api/` requests always use the live server
so stale task state is never served from its cache.

Regenerate the checked-in PNG icons after changing the source artwork with:

```bash
.venv/bin/python scripts/generate_pwa_icons.py
```

## Autonomous Project Creation

A project-creation request is converted into a validated
blueprint, registered in the durable project pipeline, and made active:

```bash
curl -X POST http://localhost:8080/api/orchestrator/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "objective":"Build a reproducible market research pipeline",
    "source":"api"
  }'
```

The default is registry-only and does not write workspace files. Set
`"scaffold":true` to explicitly authorize a deterministic starter directory at
`projects/<project_id>/`. Scaffolding creates only `README.md` and
`.friday-project.json`, rejects existing directories, and never accepts a path
from the model. In dashboard chat, the equivalent instructions are
`Create a project for ...` and `Create a project for ... and scaffold it`.

For automation that must not depend on model planning, the request may include
a complete `blueprint` matching the public schema. Duplicate generated IDs get
a numeric suffix instead of replacing an existing project.

The dashboard polls `GET /api/orchestrator/completions` and posts every unseen
finished task into the main FRIDAY chat. This also covers work triggered through
the Projects panel, chat, voice, or direct orchestrator API calls without
duplicating completion messages after a reload.

### Project Test Triggers

The active dashboard project is registered in the orchestrator database and
all three interaction paths call the same deterministic test pipeline:

- **Projects:** move a project to Execution and select **Run tests to enter Review**.
- **Chat:** enter `/test`, `run the tests`, or `test the active project`.
- **Voice:** say “run the tests” or “test the active project.”

A successful Execution-stage run advances the durable project to Review. A
failed run stays in Execution. Results from dashboard, chat, and voice are all
associated with the same project ID and appear in its Logs tab.

Run the backend suite with:

```bash
.venv/bin/python -m unittest discover -v
```

/      *
       /\
      /  \
     / *  \
    /  .   \
   /  * . * \
~~~~~~~~~~~~~~~~~
 (  °  .  °  )
