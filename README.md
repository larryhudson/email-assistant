# email-assistant

A Python backend that runs one or more email-based AI assistants on a single
stack. Each assistant has its own inbound email address, isolated durable
memory, isolated Docker sandbox workspace, and per-assistant monthly budget.
End users only ever see email — there's no UI for them. The operator uses a
local admin web UI to inspect runs, threads, memory, and cost.

This is a personal project, not a product. The first deployment runs one
assistant for one end user. Adding more assistants is a config change, not
another deployment.

> Full design lives in [`docs/superpowers/specs/2026-05-10-email-assistant-design.md`](docs/superpowers/specs/2026-05-10-email-assistant-design.md).

## How it works

Inbound mail hits Mailgun, which calls a webhook on this service. A run is
queued in Postgres and a background worker drives the agent loop:

```
Mailgun → /webhooks/mailgun/inbound (FastAPI)
        → verify signature, parse, route to assistant by inbound address
        → resolve thread (Message-ID / In-Reply-To / References)
        → persist inbound, queue run_agent(run_id)   ← returns 200 fast
        → ─────────── worker ───────────
        → BudgetGovernor: allow / send budget-limit template
        → project thread to host dir, mirror into per-assistant Docker sandbox
        → MemoryPort.recall(assistant_id, thread_id, query)  (Cognee)
        → PydanticAI agent run with tools: read/write/edit/bash/memory_search/attach_file
        → ReplyEnvelopeBuilder builds threading headers (Re:, In-Reply-To, References)
        → EmailProvider.send_reply (Mailgun)
        → RunRecorder writes run_steps + usage, queues curate_memory
```

Routing happens before the DB write so spam and misrouted mail are dropped
without queuing a job. Inbound persistence is idempotent on `(assistant_id,
provider_message_id)` so Mailgun retries don't duplicate runs.

The agent run itself is too slow for Mailgun's webhook timeout — it lives in
a Procrastinate (Postgres-backed) job. `curate_memory` runs after each
successful run to extract durable memories from the thread and persist them
back into Cognee.

### Architecture

Ports & adapters (hexagonal), grouped by capability. Each external boundary
gets a `Protocol` in `port.py` plus adapter modules next to it:

- `mail/` — `EmailProvider` (`mailgun.py`, `inmemory.py`)
- `memory/` — `MemoryPort` (`cognee.py`, `inmemory.py`)
- `sandbox/` — `AssistantSandbox` (`docker.py`, `inmemory.py`)
- `models/` — frozen pydantic data models shared across boundaries
- `db/` — SQLAlchemy 2.0 async ORM + Alembic migrations
- `domain/` — pure orchestration (router, thread resolver, budget governor, recorder)
- `runtime/` — `AssistantRuntime` wires it all together
- `web/` — FastAPI app: Mailgun webhook + admin UI (Jinja2 server-rendered)
- `jobs/` — Procrastinate task definitions (`run_agent`, `curate_memory`)

The core never imports a concrete adapter — composition wires adapters in at
the edge so tests can swap real Mailgun / Cognee / Docker for in-memory
fakes.

### Stack

| Concern | Choice |
| --- | --- |
| Language / runtime | Python 3.13 |
| Package manager | `uv` |
| Web framework | FastAPI + Jinja2 (no HTMX yet) |
| Database | Postgres (Docker) + SQLAlchemy 2.0 async + Alembic |
| Background jobs | Procrastinate |
| Agent framework | PydanticAI |
| Default model | Fireworks-hosted minimax-m2p7 (OpenAI-compatible) |
| Memory | Cognee |
| Email provider | Mailgun |
| Sandbox | Per-assistant long-lived Docker container |
| Lint / format | Ruff |
| Type checker | `ty` (Astral) — *not* mypy |
| Tests | pytest + pytest-asyncio (auto mode) |

## Getting started

### Prerequisites

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Docker (for Postgres + the agent sandbox)
- [hivemind](https://github.com/DarthSim/hivemind) (`brew install hivemind`) — runs the dev process stack
- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/) (`brew install cloudflared`) — public URL for Mailgun → local webhook
- A Mailgun account with an inbound route, plus API keys for Fireworks and OpenAI (for embeddings)

### Setup

```bash
uv sync                           # install deps + dev deps
cp .env.example .env              # then fill in real keys
```

### Run the dev stack

```bash
make dev
```

That brings up Postgres (via `docker-compose`), runs migrations, then
launches hivemind on `Procfile.dev` which runs three processes together:

- **web** — FastAPI app on `http://127.0.0.1:8000`. Admin UI at `/admin/`,
  Mailgun webhook at `/webhooks/mailgun/inbound`.
- **worker** — Procrastinate worker that picks up `run_agent` and
  `curate_memory` jobs.
- **tunnel** — `cloudflared` quick tunnel; the printed
  `https://<random>.trycloudflare.com` URL goes into Mailgun's inbound route
  action so external mail reaches the local app.

All three processes' output is also tee'd to `dev.log`.

To exercise the loop without sending real Mailgun replies, prefix the
worker line in `Procfile.dev` with `EMAIL_AGENT_WORKER_DRY_RUN=true` — it
swaps the Mailgun adapter for an in-process stub.

### Other make targets

```bash
make db-up      # start Postgres only
make db-down    # stop Postgres
make migrate    # alembic upgrade head
make test       # pytest tests/unit
```

### CLI

`uv run email-agent --help` lists the operator commands:

- `migrate` — run Alembic migrations
- `web` — start the FastAPI app
- `worker` — start a Procrastinate worker
- `seed-assistant` — provision an assistant (owner, end user, scope, budget)
- `seed-memory` — preload Cognee with seed memories for an assistant
- `inject-email` — feed an `.eml` fixture through the webhook path (handy for local repro)

## Project layout

```
src/email_agent/
  agent/         PydanticAI agent + tool wrappers
  config.py      Settings (pydantic-settings, reads .env)
  db/            SQLAlchemy ORM + Alembic migrations
  domain/        router, thread resolver, budget governor, recorder, projector
  jobs/          Procrastinate app + run_agent / curate_memory tasks
  mail/          EmailProvider port + Mailgun + in-memory adapters
  memory/        MemoryPort + Cognee + in-memory adapters
  models/        frozen pydantic wire models
  runtime/       AssistantRuntime
  sandbox/       AssistantSandbox + Docker + in-memory adapters
  web/           FastAPI app, Mailgun webhook, admin UI

tests/
  unit/          fast, isolated tests with in-memory adapters
  integration/   slower tests against real Postgres + Docker

docs/
  superpowers/specs/   design docs
  superpowers/plans/   implementation plans (sliced delivery)
```

## Domain models vs DB models

Two parallel hierarchies that diverge intentionally:

- `models/` — frozen pydantic models for transport (webhook payloads, agent
  inputs/outputs).
- `db/models.py` — SQLAlchemy ORM rows for durable Postgres state.

Each domain module that crosses the seam owns its own mapping (e.g.
`RunRecorder` writes message rows from the normalized form). No
auto-sync, no `sqlmodel`, no codegen — explicit beats magic at this size.
Round-trip tests at the seam catch drift.

## Status

Slice 1 (core data + ports) is in. Future slices are tracked under
`docs/superpowers/plans/`. This is pre-1.0; the data model and adapter
surfaces will move.

## Non-goals

- Multi-admin / CRM-style user management.
- A UI for end users.
- Untrusted senders. Each assistant has an `allowed_senders` allowlist;
  anything else is dropped.
- Calendar / payment / high-risk write tools beyond what the sandbox
  already permits via `bash`.

## License

No license file yet — treat as all-rights-reserved until one is added.
