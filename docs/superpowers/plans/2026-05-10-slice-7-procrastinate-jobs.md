# Slice 7 — Procrastinate background jobs + curate_memory

**Goal:** Close the agent-run → memory-write loop. After this slice:

- `accept_inbound` enqueues a `run_agent(run_id)` Procrastinate job instead of relying on `--follow` to invoke `execute_run` directly.
- A worker process picks up `run_agent` jobs and runs them — per-assistant serialized via Procrastinate `queueing_lock`.
- After `record_completion`, a `curate_memory(assistant_id, thread_id, run_id)` job is enqueued. The worker picks it up and persists the turn's user-message + agent-reply into the assistant's cognee memory under the thread's `session_id`.
- A second inbound on the same thread (or a related one) can recall content from the first via the existing `MemoryPort.recall` pre-call.

That closes the loop end-to-end: write on run completion → read on next run.

## Architecture

Procrastinate is Postgres-backed. It installs its own tables alongside ours via its own schema, applied with `procrastinate schema --apply` (or via Alembic). The web process owns the queue (writes), the worker process owns the dispatch (reads + executes tasks).

Two new tasks register with a single `procrastinate.App`:

| Task | Enqueued by | Body |
| --- | --- | --- |
| `run_agent(run_id)` | `AssistantRuntime.accept_inbound` | load run → `runtime.execute_run(run_id)` |
| `curate_memory(assistant_id, thread_id, run_id)` | `RunRecorder.record_completion` | load run's inbound + outbound bodies → `memory.record_turn` for each |

Per-assistant serialization for `run_agent` via `queueing_lock=f"assistant-{assistant_id}"` — Procrastinate enforces sequential execution while the lock is held, queueing additional runs for that assistant. `curate_memory` does **not** take the assistant lock; the cognee adapter's process-wide `asyncio.Lock` already serializes config-swap-sensitive calls.

`curate_memory` writes:
- `memory.record_turn(assistant_id, thread_id, "user", inbound_body)` — the user's message
- `memory.record_turn(assistant_id, thread_id, "assistant", outbound_body)` — the agent's reply

That's it for V1 — let cognee's `remember(..., session_id=thread_id)` handle session memory + auto-bridging to the durable graph. Future iterations could add tool-call traces, but the user/assistant pair is the load-bearing minimum.

## Out of scope

- `notify_budget_threshold` job — a thin wrapper, deferred to a follow-up. The infra in this slice is enough that adding it later is a small change.
- `@cognee.agent_memory` decorator on the agent run loop — superseded by `curate_memory` for write coverage. Revisit only if session traces during the run prove valuable.
- Tool-call trace ingestion into memory — out for V1 to keep the curation surface tight.
- Worker auto-restart on file changes (`watchfiles`-based) — defer to a dev-tooling pass.
- Dead-letter / max-retry policy beyond Procrastinate defaults.

## File structure

**Create:**
- `src/email_agent/jobs/__init__.py` — package marker.
- `src/email_agent/jobs/app.py` — `make_procrastinate_app(settings)` factory; module-level `app` variable for `procrastinate worker -a email_agent.jobs.app:app`.
- `src/email_agent/jobs/run_agent.py` — `@app.task(name="run_agent", queueing_lock=...)` definition. Body resolves dependencies via a small composition helper, then calls `runtime.execute_run(run_id)`.
- `src/email_agent/jobs/curate_memory.py` — `@app.task(name="curate_memory")`. Body loads the run + inbound/outbound bodies, calls `memory.record_turn` twice.
- `src/email_agent/jobs/deps.py` — `build_worker_deps(settings)` returns `(runtime, memory, session_factory)` for tasks. Avoids a circular dep between `jobs/` and `composition.py`.
- `tests/unit/jobs/test_run_agent.py` — calls the task body directly with a stub `AssistantRuntime`; asserts it routes to `execute_run`.
- `tests/unit/jobs/test_curate_memory.py` — task body called directly with `InMemoryMemoryAdapter` + a SQLite session factory; asserts both `record_turn` calls fire with the expected role/content.
- `tests/integration/test_jobs_postgres.py` — gated by `EMAIL_AGENT_E2E=1` + `DATABASE_URL` pointing at Postgres. Spins up Procrastinate against the real DB, enqueues a `run_agent` job, runs the worker briefly, asserts the run completes.

**Modify:**
- `pyproject.toml` — `uv add procrastinate`.
- `src/email_agent/runtime/assistant_runtime.py` — `accept_inbound` calls `app.defer_async("run_agent", run_id=...)` after writing the queued row. Held behind a `job_defer_callback` constructor arg so unit tests can stub it.
- `src/email_agent/domain/run_recorder.py` — `record_completion` calls a `curate_memory_defer` callback after the transaction commits.
- `src/email_agent/cli.py` — new `worker` command (replaces today's stub if any) that runs `procrastinate worker` against the configured app.
- `src/email_agent/db/migrations/` — Alembic revision that runs `procrastinate.testing.apply_schema(...)` or invokes `procrastinate schema --apply` so the procrastinate tables land alongside ours.

## Conventions

- TDD red-green-refactor, one failing test at a time. Behaviour-driven (no shape-only tests).
- Test helpers use explicit kwargs defaults — no `kw.pop`.
- Task bodies are unit-tested by calling the underlying coroutine directly. Procrastinate's queue/dispatch is tested once, end-to-end, against real Postgres (gated).
- `ty: ignore[...]` (NOT `type: ignore`) when suppressing.

## Tasks (TDD)

### Task 0 — Add procrastinate

- [ ] `uv add procrastinate`
- [ ] `uv run python -c "import procrastinate; print(procrastinate.__version__)"`
- [ ] Commit `chore(deps): add procrastinate for background jobs`

### Task 1 — Procrastinate App factory

Red: a unit test that `make_procrastinate_app(settings)` returns an app whose connector points at `settings.database_url` and that `run_agent` + `curate_memory` are registered.

Green: minimal `jobs/app.py` with a `PsycopgConnector`-backed app, two `@app.task` registrations stubbed as no-ops.

### Task 2 — `curate_memory` task body persists user + assistant turns

Red: call `curate_memory.fn(assistant_id="a-1", thread_id="t-1", run_id="r-1")` against a SQLite session factory pre-seeded with an inbound + outbound message pair, with `InMemoryMemoryAdapter` injected. Assert two `record_turn` calls landed: one with `role="user"` and the inbound body; one with `role="assistant"` and the outbound body.

Green: load the messages by `run_id`, iterate, call `memory.record_turn`.

### Task 3 — `run_agent` task body delegates to execute_run

Red: stub runtime that records `execute_run(run_id)` calls. Invoke `run_agent.fn(run_id="r-1")`. Assert one delegated call.

Green: pull a runtime from `build_worker_deps`, call `execute_run`, return outcome's `__class__.__name__` for telemetry.

### Task 4 — `accept_inbound` enqueues `run_agent`

Red: extend the existing `accept_inbound` test to assert the injected `job_defer_callback` was called once with the new `run_id` for the accepted (non-dropped) path.

Green: thread the callback through the runtime constructor, call it after the queued row commits, default to a no-op so tests that don't care don't break.

### Task 5 — `record_completion` enqueues `curate_memory`

Red: extend the recorder test to inject a `curate_memory_defer` callback and assert it was called once with `(assistant_id, thread_id, run_id)` after the recorder's transaction commits.

Green: thread the callback through `RunRecorder`, call it post-commit.

### Task 6 — Alembic migration applies Procrastinate schema

Red: a migration test that runs `alembic upgrade head` against a fresh Postgres DB and queries `pg_tables` for at least `procrastinate_jobs`.

Green: alembic revision that `op.execute()`'s the procrastinate DDL (snapshot of `procrastinate schema --apply`'s SQL).

### Task 7 — End-to-end worker integration test (gated)

Red: `EMAIL_AGENT_E2E=1` + Postgres-backed test that:
1. Composes the production runtime + worker deps.
2. Calls `accept_inbound(fixture)` → asserts a `run_agent` job is queued.
3. Spawns a Procrastinate worker for ~5s.
4. Asserts the `agent_runs` row reaches `status="completed"` and a `curate_memory` job follows.

Green: confirm the wiring matches; whatever's left is fixture/setup glue.

### Task 8 — `email-agent worker` CLI

Red: invoking `email-agent worker --help` should list it; running it should call `procrastinate worker` against the configured app.

Green: typer command that imports `jobs.app:app` and runs `app.run_worker(queues=["default"], wait=True)`.

### Task 9 — Refactor + smoke

- [ ] `uv run ruff format && uv run ruff check && uv run ty check`
- [ ] `uv run pytest tests/unit` (must stay green)
- [ ] Run a real `inject-email --follow` against the worker locally; second inject on the same thread should show recalled content from the first.
- [ ] Commit per cycle; final commit ties it together if needed.

## Open questions

- **Procrastinate connection mode**: psycopg sync vs `aiopg` async connector. Sync is simpler; async fits FastAPI's event loop better. Default to async unless something objects.
- **Worker concurrency**: default to 1 worker process for MVP. Per-assistant `queueing_lock` already prevents intra-assistant parallelism; cross-assistant parallelism can wait until there's a second assistant.
- **Migration ordering**: do we land the procrastinate tables in our Alembic chain (single `alembic upgrade head` does everything) or run them separately? Single chain is less surprising; chosen as the default unless there's a good reason to split.
- **What if cognee is unavailable when curate_memory fires?** First pass: let Procrastinate retry via its default policy. Failed-after-retries memory writes are non-fatal — log + drop. Don't block runs on curation.
