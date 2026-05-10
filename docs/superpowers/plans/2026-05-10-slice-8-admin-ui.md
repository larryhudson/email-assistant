# Slice 8 — Admin UI (MVP)

**Goal:** Give the operator (Larry) a server-rendered FastAPI + Jinja2 admin to inspect what the system did. After this slice, every accepted inbound is visible in the UI alongside its full agent trace — the inbound message, the memory recall context, every tool call's input + output, the agent's reply, status / errors, and cost. That trace view is the load-bearing piece — it's how Larry debugs prompt + memory issues without grepping JSON logs.

The slice is read-only. Pause / resume / reset / memory delete actions are deferred to a follow-up so this slice can stay scoped.

## Architecture

FastAPI mounts an `/admin` router. Routes render Jinja2 templates from `src/email_agent/web/admin/templates/`. SQLAlchemy queries reuse the same async session factory the runtime uses; nothing new at the data layer.

Three views land in this slice:

1. **Assistants list** (`GET /admin/`) — landing page. Table of assistants: id, inbound address, status, model, monthly budget, current period spend, last-run timestamp.
2. **Runs list** (`GET /admin/runs`) — paginated, newest first. Filters via query string: `?assistant_id=...&status=completed`. Columns: started_at, assistant, thread, status, cost, link to detail.
3. **Run detail** (`GET /admin/runs/{run_id}`) — the trace view. Sections (top-down):
   - Header: assistant, thread, status, started/completed, cost
   - Inbound email: from / subject / body
   - Recalled memory: the chunks returned by `MemoryPort.recall` for this run (we don't persist these today — see "Open question" below)
   - Steps: ordered list of `run_steps` rows; each shows kind (model / tool:read / tool:bash / etc), input, output, cost
   - Outbound reply: subject, body, message-id headers
   - Error: full text if `status="failed"`
4. **JSON variant** (`GET /admin/runs/{run_id}.json`) — same data as the run detail, machine-readable. Useful for tooling and `jq`-friendly debugging.

**Auth**: none in MVP. Web binds to `127.0.0.1` (already configured via `Settings.admin_bind_host`). Network isolation is the access control. A note in the index page header makes the "no auth" assumption explicit. Adding auth is a small follow-up once the surface is real.

## Open question — memory recall persistence

The design's run-detail view shows "retrieved memory" alongside the run. Today `execute_run` calls `MemoryPort.recall(...)` once and inlines the result in the agent prompt, but doesn't persist it. Two ways to make it visible:

1. **Persist recalled chunks at run time.** Add a column or a `run_memory_recalls` table that stores the per-run memory snippets. `execute_run` writes to it after `recall()`. The admin view renders rows.
2. **Re-run recall from the admin handler.** When loading run detail, re-call `MemoryPort.recall(assistant_id, thread_id, body_text)` to reconstruct what the agent saw. Decoupled but not faithful — memory may have grown / changed since the run.

(1) is more honest and supports the eventual replay/eval workflow. Picking (1) for this slice — adds a small table.

## Out of scope (deferred)

- Pause / resume assistant, reset sandbox, delete memory entries.
- Threads view (the runs view links inbound messages anyway; full thread history can wait).
- Memory view (Cognee browse/search) — non-trivial because it needs cognee user resolution + paginated listing. Worth its own slice.
- Budget view as a dedicated page (current spend already shown on assistants list, which is the key signal).
- Sandbox view (container status, recent commands).
- HTMX, any client-side interactivity, dark mode, fancy CSS — server-rendered HTML with minimal styling; one CSS file is fine.
- Auth (network-bound for MVP).

## File structure

**Create:**
- `src/email_agent/web/admin/__init__.py` — package marker
- `src/email_agent/web/admin/router.py` — `make_admin_router(...)` returns an `APIRouter`. Routes for `/`, `/runs`, `/runs/{id}`, `/runs/{id}.json`.
- `src/email_agent/web/admin/templates/base.html` — outer layout (nav, page title, content block)
- `src/email_agent/web/admin/templates/assistants.html`
- `src/email_agent/web/admin/templates/runs_list.html`
- `src/email_agent/web/admin/templates/run_detail.html`
- `src/email_agent/web/admin/static/admin.css` — minimal styling (tables, monospace for trace text)
- `src/email_agent/db/models.py` — add `RunMemoryRecall(id, run_id, content, score, created_at)` rows
- `src/email_agent/db/migrations/versions/2026_05_10_admin_run_memory_recall.py` — new alembic revision
- `src/email_agent/runtime/assistant_runtime.py` — after `memory.recall(...)`, also persist each `Memory` to `RunMemoryRecall` keyed on `run_id`
- `tests/unit/web/test_admin_router.py` — render assistants list, runs list, run detail (HTML + JSON) against SQLite fixtures
- `tests/unit/runtime/test_execute_run.py` — extend the recall-injection test to assert recalled memories also persist to the DB

**Modify:**
- `src/email_agent/web/app.py` — mount the admin router under `/admin`
- `src/email_agent/cli.py` — `web` command already exists; just confirm it serves the admin too

## Conventions

- TDD red-green-refactor, one failing test at a time.
- Tests assert on response status + key strings in the rendered HTML (fragile-by-design — if a template stops rendering the assistant id, the test fails).
- HTML structure stays minimal; templates use `{% extends "base.html" %}` and one block.
- No JS / HTMX / client-side state.
- IDs in fixtures: `uuid.uuid4().hex[:8]` with prefix.
- `ty: ignore[...]` (NOT `type: ignore`) when suppressing.

## Tasks (TDD)

### Task 0 — `RunMemoryRecall` table + migration

- [ ] Red: add a query in test_execute_run that asserts `RunMemoryRecall` rows for the run after `execute_run` completes (with a stubbed memory adapter returning two memories).
- [ ] Green: add the model in `db/models.py`, alembic revision, persist rows in `execute_run` after the recall call.
- [ ] Run `alembic upgrade head` against local Postgres to verify the migration applies.

### Task 1 — Admin router scaffolding + assistants list

- [ ] Red: `tests/unit/web/test_admin_router.py::test_assistants_list` builds a FastAPI app with the admin router, hits `GET /admin/`, asserts 200 + assistant id + inbound address present in the HTML.
- [ ] Green: `make_admin_router(session_factory, templates_dir)` returns `APIRouter`. Route loads assistants + budget + last-run timestamp; renders `assistants.html`.

### Task 2 — Runs list

- [ ] Red: seed an assistant + 3 runs (mixed statuses), hit `GET /admin/runs`, assert all 3 visible. Then `GET /admin/runs?status=completed`, assert only completed shown.
- [ ] Green: paginated query (default page_size=50), filter by `assistant_id` + `status`. Newest first. Renders `runs_list.html`.

### Task 3 — Run detail HTML

- [ ] Red: seed an assistant, an inbound, an outbound, a run with two `run_steps`, two `RunMemoryRecall` rows. Hit `GET /admin/runs/{id}`, assert: inbound subject, inbound body, both step inputs/outputs, the memory recall content, outbound body all present in HTML.
- [ ] Green: query everything, render `run_detail.html`.

### Task 4 — Run detail JSON

- [ ] Red: same fixture, hit `GET /admin/runs/{id}.json`, assert response is `application/json` and includes `inbound`, `outbound`, `steps`, `memory_recalls`, `usage` keys with the expected shape.
- [ ] Green: factor out the loading code so HTML and JSON paths share it; JSON path returns a `pydantic.BaseModel` for FastAPI to serialize.

### Task 5 — Mount admin router in the web app

- [ ] Red: hit `GET /admin/` against the full app factory output, expect 200.
- [ ] Green: `web/app.py` mounts the admin router at `/admin`. Static files at `/admin/static`.

### Task 6 — Smoke + polish

- [ ] `uv run pytest tests/unit -q` passes
- [ ] `uv run ruff format && uv run ruff check && uv run ty check`
- [ ] Run the web server locally; click through pages.
- [ ] Commit per cycle.

## Open follow-ups (not in this slice)

- Auth: at minimum HTTP basic, ideally OIDC / passwordless link.
- Action endpoints: pause / resume / reset sandbox / delete memory.
- Threads view (full email history + linked runs).
- Memory browse view (Cognee queries with pagination + delete/demote).
- Sandbox status view.
- HTMX for filter forms + live status badges.
