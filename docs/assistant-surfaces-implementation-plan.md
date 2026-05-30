# Assistant Surfaces Implementation Plan

This plan sketches how to add **Assistant Surfaces** to `email-assistant`.

An Assistant Surface is any assistant-owned HTTP interface exposed through the platform edge. It can be a dashboard, form, API endpoint for Apple Shortcuts, webhook receiver, or small assistant-authored app. The assistant owns the routes and presentation. The platform owns routing, auth, run creation, email sending, audit, and safety.

The plan is intentionally **personal-first**. It assumes one owner building for himself, with email still the primary interface. Multi-user sharing, wildcard subdomains, scoped expiring capability links, scale-to-zero, and rich audit tables can come later if real usage demands them.

## Goals

- Let assistants expose richer HTTP interfaces without adding domain-specific UI to the core app.
- Support both browser pages and direct POST/API surfaces.
- Reuse the existing assistant workspace and agent run pipeline.
- Keep the first implementation small enough to build and learn from.
- Keep privileged effects behind platform-owned APIs.

## Non-Goals For V1

- No general app builder.
- No fixed platform-owned collaboration UI.
- No wildcard DNS or per-assistant subdomains.
- No manifest format.
- No platform-mediated preview server.
- No scale-to-zero or idle container shutdown.
- No OAuth, multi-user sessions, or complex capability-token system.
- No separate queryable surface audit table unless logs prove insufficient.

## Terminology

- **Assistant Surface**: assistant-owned HTTP routes exposed to the owner or tools.
- **Surface Runtime**: platform-owned proxy/lifecycle layer that exposes a surface.
- **Assistant Tools API**: platform-owned API the assistant can call for privileged operations.
- **Surface Request**: an inbound HTTP request routed to an assistant surface.
- **Surface API Token**: simple bearer token for non-browser clients such as Apple Shortcuts.

## V1 Shape

For the first version:

- Each enabled assistant surface is an HTTP server listening on port `8000` in the assistant workspace/sandbox.
- Browser access uses the existing admin Basic Auth.
- API clients can use one static bearer token per assistant.
- Public routing is path-based:

```text
https://email-assistant.larryhudson.io/surfaces/{assistant_id}/...
```

- The platform proxies requests to the assistant's server.
- Surface-triggered agent runs use synthetic inbound email messages to reuse the existing run pipeline.
- The Assistant Tools API is HTTP and described by OpenAPI. Agents can call it with `httpx`, `curl`, JS `fetch`, or generated clients.

This favors directness over architectural completeness.

## High-Level Architecture

```text
Browser / Shortcut / webhook
  |
  v
Platform FastAPI app
  |
  |-- Authenticates request
  |-- Resolves /surfaces/{assistant_id}/...
  |-- Strips unsafe headers
  |-- Proxies to assistant server on port 8000
  v
Assistant workspace / sandbox
  |
  |-- Assistant-owned HTTP server
  |-- Assistant-owned SQLite/files
  |-- Optional calls to Assistant Tools API
  v
Platform Assistant Tools API
  |
  |-- Queue agent runs
  |-- Log events
  |-- Create surface URLs/tokens later
  |-- Draft/send email later
```

## Existing Code To Build On

- `src/email_agent/web/app.py`: FastAPI app and route mounting.
- Existing admin Basic Auth middleware.
- `AssistantRuntime.accept_inbound`: persists inbound email and queues runs.
- `AssistantRuntime.execute_run`: executes queued runs in a workspace.
- `WorkspaceProvider`: resolves a per-assistant workspace.
- `DockerWorkspaceProvider`: creates long-lived per-assistant Docker-backed workspaces.
- `AgentRun`, `EmailThread`, `EmailMessage`, `RunStep`, `UsageLedger`: existing operational records.
- Admin UI: already inspects assistants, runs, prompts, and workspace content.

## Workspace Lifecycle

For personal use, keep lifecycle simple:

- Assistant workspaces are long-lived.
- Containers start on assistant creation or first surface access.
- Containers keep running until explicitly stopped or archived.
- No idle timeout or wake-on-request machinery in v1.
- The assistant surface server is expected to listen on port `8000`.

If a container is stopped, the platform can try a simple `docker start` before proxying. Anything more elaborate can wait.

## Routing

Use path-based routing first:

```text
GET  /surfaces/{assistant_id}/
POST /surfaces/{assistant_id}/api/capture-expense
POST /surfaces/{assistant_id}/_action/run
```

Subdomains can be added later:

```text
https://budget-bot.assistant.larryhudson.io/
```

Path routing avoids wildcard DNS, wildcard TLS, cross-subdomain cookie behavior, and local-dev complexity.

Reserve platform-owned prefixes under each surface:

```text
/_action/*
/_platform/*
```

The assistant's proxied server should not receive those reserved routes.

## Auth

Keep v1 auth deliberately small:

- Browser surfaces: reuse existing admin Basic Auth.
- API surfaces: support one static bearer token per assistant.
- Public unauthenticated routes: avoid by default.

Minimal token table:

```text
surface_tokens
  id primary key
  assistant_id
  token_hash
  created_at
  revoked_at nullable
```

No scopes, expiry, route patterns, max uses, or per-end-user ownership yet.

Example Apple Shortcut request:

```http
POST /surfaces/budget-bot/api/capture-expense
Authorization: Bearer sk_budget_bot_abc123
Content-Type: application/json

{
  "amount": 14.5,
  "merchant": "Pret",
  "category": "Lunch"
}
```

## Proxy Behavior

The surface proxy should:

- Authenticate before proxying.
- Resolve `{assistant_id}`.
- Ensure the workspace/container is running.
- Proxy to the assistant server on port `8000`.
- Strip inbound `Authorization`, `Cookie`, `X-Assistant-*`, `X-Viewer-*`, and other trusted/internal headers before forwarding.
- Inject minimal trusted headers if useful:

```text
X-Assistant-Id: budget-bot
X-Surface-Auth: owner_basic | api_token
X-Surface-Request-Id: ...
```

- Enforce request body size limits.
- Enforce a request timeout.
- Log method, path, assistant id, auth mode, status, and duration.

For v1, logs are enough. Add a `surface_events` table later if queryable audit becomes useful.

## Assistant Surface Server

The assistant owns the server code. The platform assumes it is listening on port `8000`.

Static sites are just a degenerate server case:

```bash
python -m http.server 8000 --directory public
```

Or:

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/", StaticFiles(directory="public", html=True))
```

A dynamic assistant surface can use FastAPI, Flask, Node, or anything else that speaks HTTP.

No manifest is required in v1. Add `.assistant/surface.json` later if assistants need custom commands, ports, health checks, route declarations, or permissions.

## Assistant Self-Testing

The assistant should be able to test its own surface before sending links to the owner.

Minimum requirement:

- Surface server listens on `localhost:8000` inside the sandbox.
- The assistant can run smoke tests directly:

```bash
curl http://localhost:8000/
curl -X POST http://localhost:8000/api/capture-expense \
  -H 'Content-Type: application/json' \
  -d '{"amount":14.5,"merchant":"Pret"}'
```

The platform should inject the public path-based URL so the assistant can put working links in email:

```text
ASSISTANT_SURFACE_BASE_URL=https://email-assistant.larryhudson.io/surfaces/budget-bot
```

No platform-mediated preview route is needed for v1. Local server smoke tests are enough.

## Assistant Tools API

The Assistant Tools API is the assistant-facing way to ask the platform to perform privileged operations.

For v1, make the canonical contract HTTP plus OpenAPI:

```text
GET  /_internal/assistant-tools/openapi.json
POST /_internal/assistant-tools/v1/runs
POST /_internal/assistant-tools/v1/events
```

Agents can use:

- `httpx`
- `curl`
- JS `fetch`
- generated clients
- a small Python helper later, if it proves useful

Do not make a Python client library the primary contract. The OpenAPI spec is the contract.

Inside the assistant workspace, expose:

```text
ASSISTANT_TOOLS_BASE_URL=http://assistant-tools
ASSISTANT_ID=budget-bot
ASSISTANT_SURFACE_BASE_URL=https://email-assistant.larryhudson.io/surfaces/budget-bot
```

The first implementation can map `http://assistant-tools` to an internal platform route however is easiest:

- host Docker alias to the FastAPI app,
- reverse proxy inside the sandbox,
- Unix socket later,
- or temporary `host.docker.internal` URL during development.

Avoid spending much time on ambient-auth infrastructure until the surface model is proven.

### Initial Tools

Start with:

- `runs.create`: queue an agent run.
- `runs.get`: get run status.
- `events.log`: write a simple log/audit event, or just log to stdout initially.

Defer:

- `email.send`
- `email.draft`
- `approvals.create`
- `secrets.get`
- `state.get` / `state.put`
- `schedule.upsert`

Those can be added once the first surfaces demonstrate the need.

### Tool Example

```python
import httpx
import os

async def queue_run():
    async with httpx.AsyncClient(base_url=os.environ["ASSISTANT_TOOLS_BASE_URL"]) as client:
        response = await client.post("/v1/runs", json={
            "reason": "surface_capture_expense",
            "input": {
                "action": "capture_expense",
                "amount": 14.5,
                "merchant": "Pret"
            },
            "idempotency_key": "shortcut-2026-05-30T10:12:00Z"
        })
        response.raise_for_status()
        return response.json()
```

## Run Creation Strategy

Use synthetic inbound email for v1.

The existing `AgentRun` model expects `inbound_message_id`, and the runtime/admin UI already understand email-triggered runs. A surface action can create a synthetic inbound `EmailMessage` and queue a normal run.

Example synthetic message:

```text
from_email: surface@assistant.local
subject: Surface action: capture_expense
body_text:
  Source: assistant_surface
  Path: /api/capture-expense
  Payload:
  {"amount": 14.5, "merchant": "Pret"}
```

Use the surface action idempotency key as the synthetic provider message id when available. That preserves the existing duplicate-delivery protection pattern.

Later, if surface-triggered runs become central, add explicit `trigger_source`, `trigger_payload`, and nullable `inbound_message_id` fields to `agent_runs`.

## CSRF And Browser Safety

Basic Auth plus browser POST routes can create CSRF risk.

For owner-browser action routes:

- Require `Content-Type: application/json`.
- Reject cross-origin requests.
- Consider requiring a custom header such as `X-Surface-Action: 1`.
- Keep CORS closed by default.

Static bearer tokens for API clients should be sent in `Authorization: Bearer ...`, not browser cookies.

## V1 Data Model

Minimal surface settings:

```text
assistant_surfaces
  assistant_id primary key
  enabled boolean
  port integer default 8000
  created_at
  updated_at
```

Minimal API token table:

```text
surface_tokens
  id primary key
  assistant_id
  token_hash
  created_at
  revoked_at nullable
```

No `surface_events` table in v1. Use logs and existing run records.

## Suggested Milestones

### Milestone 1: Proxy Browser Surfaces

- Add `assistant_surfaces` with `assistant_id`, `enabled`, and `port`.
- Mount `/surfaces/{assistant_id}/{path:path}`.
- Reuse existing admin Basic Auth.
- Proxy to assistant workspace server on port `8000`.
- Strip unsafe headers and inject minimal trusted headers.
- Log requests.

Implementation note: the platform should not assume `127.0.0.1:{port}` is the
assistant workspace target unless explicitly configured. Docker-backed
workspaces are not published to host localhost by default, so the first
implementation uses an explicit target URL template such as
`http://{assistant_id}.surface.local:{port}` or a local-dev
`http://127.0.0.1:{port}` override.

### Milestone 2: Surface Actions To Agent Runs

- Add `POST /surfaces/{assistant_id}/_action/run`.
- Create synthetic inbound email from the action payload.
- Queue a normal `AgentRun`.
- Use idempotency key as synthetic provider message id when provided.
- Return `{ "run_id": "...", "status": "queued" }`.

### Milestone 3: Assistant Tools OpenAPI

- Add internal Assistant Tools routes for `runs.create`, `runs.get`, and maybe `events.log`.
- Publish OpenAPI at `/_internal/assistant-tools/openapi.json`.
- Expose `ASSISTANT_TOOLS_BASE_URL` and `ASSISTANT_SURFACE_BASE_URL` in the workspace.
- Document examples using `httpx` and `curl`.

### Milestone 4: API Tokens For Shortcuts

- Add minimal `surface_tokens`.
- Let admin create/revoke one token per assistant.
- Accept `Authorization: Bearer ...` on `/surfaces/{assistant_id}/api/...`.
- Document Apple Shortcuts usage.

### Later

- Wildcard subdomains.
- Signed expiring links.
- Route-level scopes.
- Queryable `surface_events`.
- Manifest file.
- Custom commands/ports.
- Dynamic lifecycle management and idle shutdown.
- Generalized `AgentRun` trigger model.

## Testing Plan

For v1, keep testing proportional:

- Unit test assistant path resolution.
- Unit test Basic Auth / bearer-token auth decisions.
- Unit test unsafe header stripping.
- Unit test synthetic inbound idempotency.
- Integration test proxying to a tiny test server on port `8000`.
- Integration test `_action/run` queues an `AgentRun`.

Manual testing is acceptable while the shape is still changing.

## First Decision Before Coding

The first implementation should commit to this v1 baseline:

```text
Path-based routing.
Assistant server on port 8000.
Existing Basic Auth for browser access.
Static bearer tokens for API clients.
Synthetic inbound email for run creation.
Assistant Tools API documented by OpenAPI.
No manifest, no static mode, no subdomains, no scale-to-zero.
```
