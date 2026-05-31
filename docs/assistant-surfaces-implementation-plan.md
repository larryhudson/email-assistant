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

## Assistant Surface Skill

The platform should seed assistant workspaces with guidance for creating and managing surfaces. Otherwise the plumbing may exist, but the assistant will not know when or how to use it.

This should probably be an assistant skill or workspace guidance file, not hard-coded app behavior.

The skill should teach the assistant:

- When a surface is useful: dashboards, repeated forms, review screens, API endpoints for Shortcuts, or state that is awkward to inspect by email.
- When email is enough: one-off answers, simple approvals, short status updates.
- How to start a surface server on `localhost:8000`.
- How to expose useful routes such as `/`, `/api/...`, or form POST handlers.
- How to test locally with `curl http://localhost:8000/`.
- How to use `ASSISTANT_SURFACE_BASE_URL` when writing links in email.
- How to keep sensitive effects behind platform actions or the Assistant Tools API.
- How to avoid implementing auth itself; the platform edge owns auth.
- How to keep the UI simple and assistant-specific rather than building a generic app.

For v1, a short seeded markdown skill is enough. Later it can include templates for:

- Static dashboard served by `python -m http.server`.
- FastAPI surface with forms and JSON endpoints.
- Apple Shortcuts endpoint.
- Surface smoke-test checklist.

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

### Milestone 5: Seed Assistant Surface Skill

- Add a workspace skill or guidance file describing when to create a surface.
- Include examples for a simple dashboard, an API endpoint, and local `curl` tests.
- Mention platform-owned auth and `ASSISTANT_SURFACE_BASE_URL`.
- Update prompts/workspace setup so assistants can discover the skill.

### Milestone 6: Production Surface Reachability

- Make Docker-backed assistant surfaces reachable without manual `/etc/hosts`
  edits or container IP assumptions.
- Give each assistant workspace a stable platform-reachable target, such as a
  Docker network alias, host-published loopback port, or provider-owned target
  resolver.
- Ensure newly created or recreated workspace containers keep the surface target
  valid.
- Add an admin or CLI path to enable/disable an `assistant_surfaces` row for an
  assistant without direct SQL.
- Document the required deployment env vars:
  `EMAIL_AGENT_SURFACE_TARGET_PROVIDER`,
  `EMAIL_AGENT_SURFACE_TARGET_URL_TEMPLATE`,
  `EMAIL_AGENT_ASSISTANT_SURFACE_BASE_URL_TEMPLATE`,
  `EMAIL_AGENT_ASSISTANT_TOOLS_BASE_URL`, and
  `EMAIL_AGENT_ASSISTANT_TOOLS_TOKEN`.
- Add a smoke test or operational check that distinguishes:
  surface not enabled, target not configured, container unreachable, and surface
  server not listening.

Deployment env for Docker-backed surfaces:

```text
EMAIL_AGENT_SANDBOX_DOCKER_NETWORK=email-agent
EMAIL_AGENT_SURFACE_TARGET_PROVIDER=docker
EMAIL_AGENT_ASSISTANT_SURFACE_BASE_URL_TEMPLATE=https://email-assistant.example.com/surfaces/{assistant_id}
EMAIL_AGENT_ASSISTANT_TOOLS_BASE_URL=https://email-assistant.example.com/_internal/assistant-tools
EMAIL_AGENT_ASSISTANT_TOOLS_TOKEN=<shared internal bearer token>
```

With `EMAIL_AGENT_SURFACE_TARGET_PROVIDER=docker`, the FastAPI process resolves
the assistant workspace container through Docker inspect at request time and
first uses a host-published loopback port when Docker exposes the configured
surface port. Docker workspace containers publish port 8000 on `127.0.0.1` with
an ephemeral host port by default, so the current host-run `make dev`
deployment works on Docker Desktop without manual `/etc/hosts` edits or
container bridge IP assumptions. If no published port exists, the resolver
falls back to the container's current IP on `EMAIL_AGENT_SANDBOX_DOCKER_NETWORK`.
If the FastAPI app itself is containerized on the same user-defined Docker
network, operators may instead use the explicit template provider:

```text
EMAIL_AGENT_SURFACE_TARGET_PROVIDER=template
EMAIL_AGENT_SURFACE_TARGET_URL_TEMPLATE=http://email-agent-sandbox-{assistant_id}:{port}
```

The workspace provider attaches each assistant container to that network with
the stable alias `email-agent-sandbox-{assistant_id}`, so template-based target
resolution does not depend on hard-coded container IPs.

Operators can enable or disable a surface without SQL:

```bash
email-agent surface-enable a-812ca3e5 --port 8000
email-agent surface-disable a-812ca3e5
```

Smoke-check the public proxy path with admin auth:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  https://email-assistant.example.com/surfaces/a-812ca3e5/_check
```

The check reports distinct failures for a disabled surface, missing target
template, unreachable Docker target, and a reachable container with no server
listening on the configured port.

Outbound assistant replies may include the safe placeholder
`${ASSISTANT_SURFACE_BASE_URL}`. The platform replaces it before sending email
with the same assistant-specific public URL written to `/workspace/.assistant/env`.
Do not substitute internal Assistant Tools placeholders in outbound user email
by default; those URLs are for privileged workspace/platform calls, not public
links.

V1 stays path-based: `/surfaces/{assistant_id}/...`. To make simple
assistant-owned HTML apps work behind that prefix, the surface proxy rewrites
root-relative values in common `text/html` attributes before returning the
response to the browser:

- `form action`
- `a href`
- `script src`
- `link href`
- `img src`

Only single-slash root-relative values are rewritten. Protocol-relative URLs,
absolute URLs, fragments, `mailto:`, `data:`, and similar values are left
alone. The proxy does not rewrite arbitrary JavaScript; JS `fetch("/api/...")`
should use relative URLs or an explicit `ASSISTANT_SURFACE_BASE_URL` for V1.

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
