# Email Assistant — Design

## Context

A Python backend that runs one or more email-based AI assistants on a single stack. Each assistant has its own inbound email address, isolated memory, isolated sandboxed workspace, and per-assistant budget. The first deployment has Larry as admin/operator and Larry's mum as the only end user. Larry can add his own assistant later as a config change, not a second deployment.

The end user only ever sees email. The admin uses a web UI to inspect runs, threads, memory, and cost.

## Goals

1. Receive inbound emails via Mailgun webhook, route to the correct assistant by inbound address.
2. Run a PydanticAI agent inside a per-assistant Docker sandbox with `read`/`write`/`edit`/`bash` + `memory_search` + `attach_file` tools.
3. Maintain isolated thread history, durable memory (Cognee), tools, budget, and run logs per assistant.
4. Send a reply through the email provider adapter, then record the run and trigger background memory curation.
5. Enforce a monthly budget per assistant; respond with a cheap template reply when exceeded.
6. Provide an admin web UI sufficient to inspect what happened and pause/approve runs.
7. Keep the email provider and memory layer behind ports so they can be swapped or stubbed for tests.

## Non-goals

- Multi-admin permissions, CRM-style user management.
- Any UI for the end user.
- Untrusted senders. Each assistant has an `allowed_senders` allowlist; anything else is dropped.
- Calendar/payment/high-risk write tools beyond what the sandbox already permits via bash.
- Sophisticated memory ranking beyond what Cognee provides out of the box.

## Architectural decisions

| Decision | Choice |
| --- | --- |
| Database | Self-hosted Postgres (Docker) |
| Web framework | FastAPI + Jinja2 server-rendered admin (no HTMX initially) |
| Background jobs | Procrastinate (Postgres-backed) |
| Memory adapter | Cognee |
| Email provider | Mailgun (first adapter) |
| Agent framework | PydanticAI |
| Default model | DeepSeek V4 Flash via OpenAI-compatible endpoint |
| Sandbox | Per-assistant long-lived Docker container, ephemeral processes per run |
| Network policy | Full internet access from sandbox; mitigated by trusted-sender allowlist + resource limits + per-run wall-clock timeout |
| First-assistant budget | $10/month, alert at 70% |

## Runtime flow

The agent run is too slow to fit inside Mailgun's webhook timeout, so the flow is split into a fast webhook path and a Procrastinate background job.

### Webhook fast path

```
Mailgun webhook
  → EmailProvider.verify_webhook(req)
  → EmailProvider.parse_inbound(req)            → NormalizedInboundEmail
  → AssistantRouter.resolve(email)              # unknown address / paused / sender not allowed → 200 + drop
  → ThreadResolver.resolve(email, scope)
  → persist inbound message + agent_runs(status="queued")   (atomic, idempotent on provider_message_id)
  → enqueue Procrastinate job: run_agent(run_id)
  → return 200
```

Routing and sender-allowlist checks happen here so spam and misrouted mail are rejected without DB writes for the agent run. Inbound message persistence is idempotent on `(assistant_id, provider_message_id)` so Mailgun retries don't enqueue duplicate jobs.

### `run_agent` job (Procrastinate worker)

```
run_agent(run_id):
  → load agent_run + inbound email + assistant scope
  → BudgetGovernor.decide(scope, ledger)
      ├─ BudgetLimitReply → send template via EmailProvider, mark run "budget_limited", done
      └─ Allow → continue
  → EmailWorkspaceProjector.project(thread, scope)
  → AssistantSandbox.ensure_started(assistant_id)
  → AssistantSandbox.project_attachments(...)
  → MemoryPort.recall(assistant_id, thread_id, query)
  → AssistantAgent.run(scope, current_message_path, memory_context, deps)
  → read pending attachment bytes out of sandbox
  → ReplyEnvelopeBuilder.build(inbound, thread, body, attachments)
  → EmailProvider.send_reply(envelope)
  → RunRecorder.record_completion(run_id, outbound, steps, usage)
      └─ enqueues curate_memory(assistant_id, thread_id, run_id) job
```

`AssistantRuntime` is the entry point for this job — it owns the order, error handling, and per-run wall-clock timeout. Agent failures still produce a recorded run with status `failed` and an admin-visible error; whether to send a "something went wrong" reply is a per-assistant policy (default off in MVP).

### Background jobs

| Job | Triggered by | Purpose |
| --- | --- | --- |
| `run_agent(run_id)` | webhook | the full agent run, reply, and recording |
| `curate_memory(assistant_id, thread_id, run_id)` | end of `run_agent` | persist Cognee session traces, extract durable memories |
| `notify_budget_threshold(assistant_id)` | inside `RunRecorder` when crossing 70% / 100% | email Larry |

Procrastinate handles retries, scheduling, and dead-letter behaviour for these.

## Modules

### Ports

#### `EmailProvider`

```python
class EmailProvider(Protocol):
    async def verify_webhook(self, request: WebhookRequest) -> None: ...
    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail: ...
    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail: ...
```

`NormalizedInboundEmail` preserves: provider message ID, `Message-ID`, `In-Reply-To`, `References`, from address, to/recipient addresses, subject, plain text body, optional HTML body, attachments metadata + bytes, received timestamp.

Adapters:
- `MailgunEmailProvider` (real)
- `InMemoryEmailProvider` (tests; captures sent replies for assertions)

#### `MemoryPort`

```python
class MemoryPort(Protocol):
    async def recall(
        self, assistant_id: str, thread_id: str, query: str
    ) -> MemoryContext: ...

    async def record_turn(
        self, assistant_id: str, thread_id: str, role: str, content: str
    ) -> None: ...

    async def search(
        self, assistant_id: str, query: str
    ) -> list[Memory]: ...

    async def delete_assistant(self, assistant_id: str) -> None: ...
```

Invariant: every operation receives `assistant_id`. Adapters must enforce scope isolation and never return memory from another assistant.

Adapters:
- `CogneeMemoryAdapter` (real) — uses `cognee.remember`, `cognee.search`, `@cognee.agent_memory(session_id=thread_id, save_session_traces=True)` for auto-curation. Per-assistant isolation: separate `data_root_directory` and `system_root_directory` rooted at `data/cognee/<assistant_id>/`. Cognee config is module-global, so the adapter holds a process-wide `asyncio.Lock` and switches config under the lock around each cognee call. (Per-assistant `queueing_lock` in Procrastinate already serializes `run_agent` jobs per assistant, but `curate_memory` jobs and admin reads can race; the global lock makes that safe.)
- `InMemoryMemoryAdapter` (tests) — dict keyed by `(assistant_id, thread_id)`.

#### `AssistantSandbox`

```python
class AssistantSandbox(Protocol):
    async def ensure_started(self, assistant_id: str) -> None: ...
    async def project_emails(self, assistant_id: str, files: list[ProjectedFile]) -> None: ...
    async def project_attachments(self, assistant_id: str, run_id: str, files: list[ProjectedFile]) -> None: ...
    async def run_tool(self, assistant_id: str, run_id: str, call: ToolCall) -> ToolResult: ...
    async def read_attachment_out(self, assistant_id: str, run_id: str, path: str) -> bytes: ...
    async def reset(self, assistant_id: str) -> None: ...
```

`ToolCall` covers `read`, `write`, `edit`, `bash`, `attach_file`. `memory_search` is **not** routed through the sandbox — it's served by the runtime against `MemoryPort` so memory bytes never enter the container.

Adapters:
- `DockerSandbox` (real) — long-lived container per assistant, started lazily, stopped after 30 min idle, filesystem persists. Volume mount for `/workspace`. Resource limits: 1 CPU, 512 MB RAM, 2 GB disk quota. Per-tool-call wall-clock timeout (e.g. 60s for bash). Per-run wall-clock budget (e.g. 5 min total).
- `InMemorySandbox` (tests) — temp directory + direct subprocess, no docker.

Base image: `python:3.13-slim` plus `curl`, `git`, `ripgrep`, `jq`, `poppler-utils`. Agent can `apt install` more.

### Domain modules

#### `AssistantRouter`

Input: `NormalizedInboundEmail`. Output: `AssistantScope` or typed rejection.

Resolves the inbound `to` address to an `assistants` row, loads owner/admin/end-user/budget/memory namespace/tool allowlist/`allowed_senders`. Rejects:
- unknown inbound address → drop, log
- assistant paused/disabled → drop, log
- sender not in `allowed_senders` → drop, log

#### `BudgetGovernor`

Input: `AssistantScope`, current usage ledger, planned run estimate.

Output: `Allow` | `BudgetLimitReply` | `RequireApproval` | `Degrade` (latter two not used in MVP but reserved).

MVP behaviour: if monthly spend ≥ limit, return `BudgetLimitReply` with days-until-reset. Reply is sent via a cheap template (no model call).

#### `ThreadResolver`

Input: `NormalizedInboundEmail`, `AssistantScope`. Output: `EmailThread` row.

Resolution order:
1. Match provider thread/conversation ID if available.
2. Match `In-Reply-To` against `message_index` for this assistant.
3. Match `References` against `message_index` for this assistant.
4. Create new thread.

Indexes both inbound and outbound `Message-ID` values scoped by `assistant_id`. Cross-assistant lookups must not match.

#### `EmailWorkspaceProjector`

Input: thread + assistant scope. Output: side-effect — writes deterministic file structure to a per-assistant host directory that is bind-mounted read-only into the container at `/workspace/emails/`. The directory is wiped and regenerated before every run, so the agent always sees current truth from the DB:

```
/workspace/emails/
  <thread-id>/
    thread.md                            # subject, participants
    NNNN-YYYY-MM-DD-from-<who>.md        # one file per email, ordered
    attachments/
      NNNN-<original-filename>
```

Plus a `current_message_path` string passed to the agent prompt pointing at the file representing the current inbound. Re-projected on every run; emails the agent edits would just be wiped and rewritten next run, so the directory is mounted read-only.

#### `AssistantAgent`

Wraps a PydanticAI `Agent`. One `Agent` instance per assistant, cached for the process lifetime. Per-run state flows through `RunContext[AgentDeps]`.

```python
@dataclass
class AgentDeps:
    assistant_id: str
    run_id: str
    thread_id: str
    sandbox: AssistantSandbox
    memory: MemoryPort
    pending_attachments: list[PendingAttachment]   # mutated by attach_file tool

agent = Agent(
    model=model_for(assistant.model),    # DeepSeek via OpenAI-compatible wrapper
    deps_type=AgentDeps,
    output_type=str,                      # the reply body
    instructions=assistant.system_prompt,
)

@agent.tool
async def read(ctx: RunContext[AgentDeps], path: str) -> str: ...

@agent.tool
async def write(ctx: RunContext[AgentDeps], path: str, content: str) -> None: ...

@agent.tool
async def edit(ctx: RunContext[AgentDeps], path: str, old: str, new: str) -> None: ...

@agent.tool
async def bash(ctx: RunContext[AgentDeps], command: str) -> BashResult: ...

@agent.tool
async def memory_search(ctx: RunContext[AgentDeps], query: str) -> list[Memory]: ...

@agent.tool
async def attach_file(ctx: RunContext[AgentDeps], path: str, filename: str | None = None) -> None: ...
```

The first four route through `ctx.deps.sandbox`; `memory_search` calls `ctx.deps.memory.search(ctx.deps.assistant_id, query)`; `attach_file` appends to `ctx.deps.pending_attachments`. The runtime reads attachment bytes out of the sandbox after `agent.run()` returns.

Each inbound email is a single `agent.run(prompt, deps=...)`. PydanticAI handles the internal tool-call loop automatically. No cross-email `message_history` — the workspace's email files are the conversation record.

The prompt passed to `agent.run` includes: the path to the current message file inside `/workspace`, recent memory recall, and run constraints (max steps, timeout).

Output: `AgentReply(body=result.output, attachments=deps.pending_attachments)`.

**Model support:** PydanticAI lists DeepSeek as an OpenAI-compatible provider, configured via the OpenAI provider class with a custom `base_url` and API key. The `model_for(name)` helper hides this; assistants reference models by short name (e.g. `"deepseek-flash"`).

#### `ReplyEnvelopeBuilder`

Input: inbound email, thread, agent body text, attachment bytes (already pulled out of sandbox). Output: `NormalizedOutboundEmail` with `Message-ID`, `In-Reply-To` set to inbound `Message-ID`, `References` built from inbound references + inbound `Message-ID`, `Re: ` subject when needed, recipients.

#### `RunRecorder`

Input: `CompletedRun`. Side effects (single transaction where possible):
- store inbound + outbound `email_messages`
- update `message_index`
- update `agent_runs` status/timestamps/reply_message_id
- write `run_steps`
- write `usage_ledger` entry
- enqueue Procrastinate job: `curate_memory(assistant_id, thread_id, run_id)`

Idempotent on `(provider_message_id, assistant_id)` so duplicate webhooks don't double-record.

#### `AssistantRuntime`

Top-level orchestrator with two entry points:

```python
async def accept_inbound(self, email: NormalizedInboundEmail) -> AcceptOutcome
    # webhook fast path: route, persist, enqueue. Returns quickly.

async def execute_run(self, run_id: str) -> RunOutcome
    # Procrastinate worker entry: budget, project, agent, reply, record.
```

Owns the order, error handling, and per-run wall-clock timeout. Guarantees every accepted email produces a row in `agent_runs` (including budget-limited and failed runs). Errors during agent execution produce a recorded run with status `failed` and an admin-visible error; whether to send a fallback reply is a per-assistant policy (default off in MVP).

The webhook handler shrinks to:

```python
@app.post("/webhooks/mailgun")
async def webhook(req: Request):
    await provider.verify_webhook(req)
    email = await provider.parse_inbound(req)
    await runtime.accept_inbound(email)
    return Response(status_code=200)
```

The Procrastinate worker registers `execute_run` as the handler for the `run_agent` job.

## Data model

Postgres tables (Alembic-managed):

```
owners(id, name, primary_admin_id, billing_scope)
admins(id, owner_id, email, role)
end_users(id, owner_id, email, display_name)
assistants(id, end_user_id, inbound_address, status, allowed_senders, model, system_prompt, created_at)
assistant_scopes(assistant_id, memory_namespace, tool_allowlist, budget_id)

email_threads(id, assistant_id, end_user_id, root_message_id, subject_normalized, created_at, updated_at)
email_messages(id, thread_id, assistant_id, direction, provider_message_id,
               message_id_header, in_reply_to_header, references_headers,
               from_email, to_emails, subject, body_text, body_html, created_at)
email_attachments(id, message_id, filename, content_type, size_bytes, storage_path)
message_index(assistant_id, message_id_header, thread_id, provider_message_id)

agent_runs(id, assistant_id, thread_id, inbound_message_id, reply_message_id,
           status, error, started_at, completed_at)
run_steps(id, run_id, kind, input_summary, output_summary, cost_cents, created_at)
usage_ledger(id, assistant_id, run_id, provider, model, input_tokens, output_tokens,
             cost_cents, budget_period, created_at)

budgets(id, assistant_id, monthly_limit_cents, period_starts_at, period_resets_at)
```

`memories` is **not** an app-level table — Cognee owns durable memory storage. The app stores enough operational data (`agent_runs`, `run_steps`, `email_messages`, `usage_ledger`) for the admin UI and budget enforcement without depending on Cognee's internals.

Procrastinate also installs its own tables in the same Postgres database.

Per-assistant on-disk artefacts (host filesystem, not Postgres):
- `data/sandboxes/<assistant_id>/workspace/` — Docker volume for `/workspace`
- `data/cognee/<assistant_id>/` — Cognee data + system root
- `data/run_inputs/<run_id>/emails/` — per-run projection (read-only mount source)

## Tools the agent gets

| Tool | Implementation |
| --- | --- |
| `read(path)` | Routed to sandbox; reads file inside container |
| `write(path, content)` | Routed to sandbox; rejects writes under `/workspace/emails/` |
| `edit(path, old, new)` | Routed to sandbox; same restriction |
| `bash(command)` | Routed to sandbox; per-call timeout |
| `memory_search(query)` | `MemoryPort.search(assistant_id, query)` — does not enter the container |
| `attach_file(path, filename?)` | Records pending attachment; runtime reads bytes out post-run |

The agent gets `memory_search` (read) but deliberately no write-side counterpart (`remember`, `save_fact`, etc). Memory writes happen out-of-band after the run completes — the `curate_memory` Procrastinate job extracts what's worth keeping from the recorded run + Cognee session traces. Asking the agent to decide mid-run what to persist is less reliable than letting curation see the whole turn (final reply, tool outputs, errors) and pick durable facts from that. The asymmetry is intentional.

The agent navigates email history by `read`/`bash`-ing files under `/workspace/emails/`, not via a separate `inspect_thread` tool.

## Admin UI (server-rendered FastAPI + Jinja2)

Views:

1. **Assistants** — status, inbound address, monthly budget, spend this period, pause/resume, reset sandbox.
2. **Runs** — filter by assistant/thread/date/status/cost. Detail view: inbound email, retrieved memory, tool calls + outputs, outbound reply, status/errors, cost.
3. **Threads** — full email history, threading headers, related runs.
4. **Memory** — durable memories per assistant (queries Cognee), delete/demote, source links.
5. **Budget** — limit, reset date, threshold alerts, cost breakdown.
6. **Sandbox** — per-assistant: container status, recent commands, manual reset.

No auth in MVP beyond the admin app being only reachable on Larry's network. (Followup: add basic auth.)

## Cost controls

- `BudgetGovernor` runs before any model/tool call.
- `usage_ledger` records token counts and estimated cost per run.
- Hard stop at limit; cheap template budget-limit reply.
- Admin notification when spend crosses thresholds (e.g. 70%).
- Per-run wall-clock timeout (5 min) caps runaway loops independent of token spend.

## Development methodology

**Red-green-refactor TDD throughout.** For each new behaviour:

1. **Red:** write a failing test against the module's interface. The test must fail for the right reason — run it and read the failure before writing implementation.
2. **Green:** write the smallest implementation that makes the test pass. No extra features, no anticipatory abstractions.
3. **Refactor:** improve the structure with the test as a safety net. Tests stay green.

Constraints:

- **Tests live at the module interface, not at adapter internals.** If a test only passes for the Cognee adapter, it belongs as an adapter-specific test, not a memory-port test.
- **One failing test at a time.** Don't write a batch of tests then implement. Each test → implementation → refactor cycle is a single commit's worth of progress (commit per cycle is fine but not required).
- **The failure message is part of the test.** A green test that would have stayed green if you deleted the implementation is no test.
- **Refactor with intent.** Don't refactor in the same step as adding behaviour. After green, decide whether the design needs help; if yes, refactor; if no, move to the next test.
- **Adapters use real dependencies in integration tests** (real Postgres, real docker, real Cognee with a tmp data root) — not mocks. The whole point of the port seam is that you can write the contract test once and reuse it for all adapters.

Implementation slice work proceeds slice-by-slice in TDD. Each slice has its own failing-test-first list before any implementation begins; the implementation plan (next document) will spell those out.

## Feedback loops

The build → test → improve loop is the project's most important deliverable beyond the assistant itself. Speed targets:

| Loop | Target | Trigger |
| --- | --- | --- |
| Module unit tests | < 2s | `pytest -k <module>` on save |
| Agent behaviour | ~30s | `inject-email --follow` |
| Replay run with tweaked config | seconds–minute | `rerun <run-id> --model … --system-prompt …` |
| Eval over fixture corpus | minutes | `email-assistant eval` |
| Sandbox debugging | instant | `sandbox shell <assistant-id>` |
| Production observability | real-time | admin run trace view + JSON logs |

### Investments baked into the build

1. **`inject-email --follow`.** Beyond the basic inject command, `--follow` waits for the resulting `run_agent` job to complete and prints the rendered reply, full tool trace, model token usage, and cost summary inline. Eliminates the admin UI round-trip during prompt iteration.

2. **`email-assistant rerun <run-id>` command** with `--model`, `--system-prompt <path>`, and `--memory-isolated` flags. Re-executes the captured inbound against an alternative config. Critical for iterating prompts against a real interaction.  `--memory-isolated` runs against a tmp Cognee root so durable memory doesn't pollute the comparison.

3. **PydanticAI `TestModel` for agent unit tests.** PydanticAI ships `TestModel` (and `FunctionModel`) for scripting tool-call sequences without an API key. All unit tests of the agent loop and tool dispatch use this. Sub-second, deterministic.

4. **Eval CLI.** `email-assistant eval [--corpus path]` walks `tests/fixtures/scenarios/<name>/`, each containing `inbound.eml` and `assertions.yaml` (e.g. `reply_contains: "..."`, `tools_called: [bash, memory_search]`, `cost_under_cents: 5`). Prints a per-scenario pass/fail table with token usage. Failed scenarios become regression fixtures.

5. **Admin run trace view.** Single page showing the full picture: rendered prompt sent to the model (with memory recall context inlined), every tool call + output, the model's final response, per-step token usage and cost, latency per step. Available as both HTML and JSON (`/admin/runs/<id>.json`).

6. **`email-assistant sandbox shell <assistant-id>`.** Wraps `docker exec -it … bash`. Drops the operator into the assistant's actual workspace for hand debugging.

7. **Structured logs with `run_id` propagation.** stdlib `logging` configured with a JSON formatter; `run_id` and `assistant_id` propagated via `contextvars`. `jq 'select(.run_id == "abc")' logs.jsonl` recovers a full trace from logs alone.

8. **Reload knobs.** `uvicorn --reload` for web. Worker auto-restart via `watchfiles` (`watchfiles "python -m email_assistant.worker" src/`). Config watch for assistant rows: a row update via CLI invalidates the in-process `Agent` cache so the next run picks up the new system prompt without restart.

9. **Fixture-driven offline path is the default for dev.** Real Mailgun deliveries are only used when actively testing the Mailgun adapter itself. All other dev iterates from `tests/fixtures/emails/*.eml`.

## Testing strategy

Tests are written against module interfaces, not adapter internals.

Priority tests:

- Mailgun webhook normalizes to `NormalizedInboundEmail` (provider-specific test).
- `AssistantRouter` maps inbound address to correct assistant.
- `AssistantRouter` rejects sender not in `allowed_senders`.
- `BudgetGovernor` returns `BudgetLimitReply` when limit is reached.
- `ThreadResolver` matches replies via outbound assistant `Message-ID`; never crosses assistant scope.
- `ReplyEnvelopeBuilder` preserves `In-Reply-To` and `References`.
- `RunRecorder` is idempotent for duplicate provider webhook delivery.
- `MemoryPort` never returns memory from another assistant (enforced by both adapters).
- `AssistantSandbox` rejects writes/edits under `/workspace/emails/`.
- `AssistantRuntime.accept_inbound` enqueues exactly one `run_agent` job per unique `(assistant_id, provider_message_id)` pair, even with duplicate webhook delivery.
- `AssistantRuntime.execute_run` end-to-end test using `InMemoryEmailProvider`, `InMemoryMemoryAdapter`, `InMemorySandbox`, fake LLM — confirms the full pipeline produces a recorded run, sent reply, and curation job for any queued inbound.

## Implementation slices

1. **Core data + ports** — normalized email models, port protocols, Postgres schema (Alembic), in-memory adapters.
2. **Mailgun inbound + threading** — webhook verification, parser, `AssistantRouter`, `ThreadResolver`, `message_index`. Stop at storing the inbound message; no agent yet.
3. **Budget + template replies** — `BudgetGovernor`, `usage_ledger`, budget-limit template reply via `MailgunEmailProvider.send_reply`.
4. **Sandbox** — `DockerSandbox`, `EmailWorkspaceProjector`, tool dispatcher.
5. **PydanticAI agent runtime** — `AssistantAgent`, `AssistantRuntime`, `ReplyEnvelopeBuilder`, `RunRecorder`. End-to-end with no memory recall (placeholder MemoryContext).
6. **Cognee memory adapter** — `CogneeMemoryAdapter` implementing recall + per-thread session memory + auto-curation.
7. **Procrastinate background jobs** — `curate_memory` job, threshold notifications.
8. **Admin UI** — assistants, runs, threads, memory, budget, sandbox views.

## Implementation details

### Repo layout

```
email-assistant/
  src/email_assistant/
    models/                 # NormalizedInboundEmail, AgentReply, AssistantScope, ToolCall, ...
    ports/                  # EmailProvider, MemoryPort, AssistantSandbox protocols
    adapters/
      mailgun/              # MailgunEmailProvider
      cognee/               # CogneeMemoryAdapter
      docker_sandbox/       # DockerSandbox
      inmemory/             # in-memory adapters for tests
    domain/
      router.py             # AssistantRouter
      budget_governor.py    # BudgetGovernor
      thread_resolver.py    # ThreadResolver
      workspace_projector.py
      reply_envelope.py
      run_recorder.py
    agent/                  # AssistantAgent (PydanticAI wrapper) + tool fns
    runtime/                # AssistantRuntime
    jobs/                   # Procrastinate job definitions
    web/
      webhook.py            # FastAPI webhook handler
      admin/                # Jinja templates + admin views
    db/
      models.py             # SQLAlchemy 2.0 async ORM
      migrations/           # Alembic
    config.py               # pydantic-settings
    cli.py                  # typer commands
    main.py                 # FastAPI app factory + worker entry
  tests/
    unit/
    integration/
    fixtures/emails/        # .eml files for offline dev
  docker-compose.yml
  docker-compose.dev.yml
  pyproject.toml
```

ORM: **SQLAlchemy 2.0 async** + Alembic. CLI: **typer**.

One file per `domain/` module — they each grow.

### Configuration

`pydantic-settings` reading `.env`. All operator/secret config in env, all per-assistant config in DB.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__")

    database_url: PostgresDsn

    mailgun_signing_key: SecretStr
    mailgun_api_key: SecretStr
    mailgun_domain: str
    mailgun_webhook_url: HttpUrl

    deepseek_api_key: SecretStr
    deepseek_base_url: HttpUrl = "https://api.deepseek.com/v1"

    cognee_llm_api_key: SecretStr
    cognee_embedding_api_key: SecretStr
    cognee_embedding_model: str = "text-embedding-3-small"

    sandbox_image: str = "email-assistant-sandbox:latest"
    sandbox_data_root: Path = Path("data/sandboxes")  # HOST path, not worker-internal
    sandbox_idle_shutdown_minutes: int = 30
    sandbox_run_timeout_seconds: int = 300
    sandbox_bash_timeout_seconds: int = 60
    sandbox_memory_mb: int = 512
    sandbox_cpu_cores: float = 1.0

    attachments_root: Path = Path("data/attachments")
    cognee_data_root: Path = Path("data/cognee")
    run_inputs_root: Path = Path("data/run_inputs")

    admin_bind_host: str = "127.0.0.1"
    admin_bind_port: int = 8001
```

### Concurrency

Per-assistant serialization for `run_agent` jobs via Procrastinate `queueing_lock=f"assistant-{assistant_id}"`. Procrastinate queues additional jobs and runs them sequentially when the lock is held.

`curate_memory` jobs do **not** take the assistant lock — they only touch Cognee, which is already serialized by the adapter's global lock. Admin reads also go through the adapter's lock.

### Sandbox control plane

Worker container mounts `/var/run/docker.sock`. Worker uses the Python `docker` SDK to start, stop, and `exec` per-assistant containers. Bind-mount sources passed to the docker daemon are **host paths**, so `Settings.sandbox_data_root` (and similar) must be host paths. The docker-compose file mounts `./data` to the same absolute path inside the worker so the path resolves identically on both sides.

Trade-off: docker-socket access is effectively host-root. Acceptable for a single-operator self-hosted MVP. The `InMemorySandbox` adapter avoids docker entirely for tests.

### Local dev

Two complementary paths:

- **Tailscale Funnel** for end-to-end testing against real Mailgun. This is machine-level setup, not part of the Hivemind process stack.
- **`email-assistant inject-email <fixture.eml> --to <inbound-address>` CLI** for the 90% of dev that doesn't need real Mailgun. Parses an `.eml`, constructs a `NormalizedInboundEmail` directly, and calls `runtime.accept_inbound`. Fixtures under `tests/fixtures/emails/`.

A `docker-compose.dev.yml` provides hot-reload (`uvicorn --reload`) and a worker with auto-restart.

### Bootstrap CLI

`typer`-based `email-assistant` command:

| Command | Purpose |
| --- | --- |
| `init` | Read `.env`, create owner row + admin row for operator email. Idempotent. |
| `create-end-user --email --name` | Create an end user. |
| `create-assistant --end-user --inbound-address --model --monthly-budget --allowed-senders --system-prompt-file` | Create assistant + budget + assistant_scope. |
| `pause-assistant <id>` / `resume-assistant <id>` | Toggle status. |
| `reset-sandbox <id>` | Stop and recreate the container; wipes `/workspace`. |
| `inject-email <path> --to <address>` | Local dev: inject a fixture .eml as inbound. |
| `migrate` | `alembic upgrade head`. |
| `worker` | Run the Procrastinate worker. |
| `web` | Run uvicorn for webhook + admin. |

### Email body handling

Inbound:
- Store both `body_text` and `body_html` from Mailgun's parsed payload as-received. No quoted-reply stripping, no signature stripping — the workspace already gives the agent structured access to prior messages, so the trade-off favours fidelity.
- `EmailWorkspaceProjector` writes:
  - `<NNNN-…>.md` — frontmatter (from/to/date/subject/headers + `html: <NNNN-…>.html` if HTML companion exists) + plain body.
  - `<NNNN-…>.html` — only when HTML body is present. Lets the agent fall back to the HTML version when the plain text is mangled (e.g. forwarded newsletters).

Outbound:
- Plain text only for MVP. Mum's mail client renders it fine.

### Memory recall query

`AssistantRuntime.execute_run` calls `MemoryPort.recall(assistant_id, thread_id, query=truncate(inbound.body_text, 2000))` once before agent invocation, and includes the result in the prompt. Trade-off: an extra Cognee call on every run regardless of whether memory is relevant, but reliable behaviour. Revisit by replacing `truncate` with a small LLM-extracted query (option B) if recall quality becomes a problem.

The agent additionally has the `memory_search` tool for ad-hoc lookups during the run.

## Open items deferred from MVP

- Admin UI auth (start with network-only access).
- Allowlist egress from sandbox (start with full internet).
- Multi-admin permissions.
- Approval-required mode for higher-risk runs.
- Adding Larry's own assistant (config-only change once MVP works for Mum).
