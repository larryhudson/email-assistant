# Assistant Surfaces Synthesis

This document summarizes the second round of brainstorming about `email-assistant`, focused on the direction now called **Assistant Surfaces**.

- Keep assistant creation and shaping flexible.
- Avoid adding heavy product-specific workflow UI to the central app.
- Let an assistant expose its own HTTP surface when a richer interface is useful.
- Keep the surface assistant-controlled, while the platform owns routing, auth, privileged effects, audit, and safety.

An Assistant Surface is any assistant-owned HTTP interface exposed through the platform edge. It can be a browser page, dashboard, form, API endpoint for Apple Shortcuts, webhook receiver, or small assistant-authored app.

The motivating example is an assistant that can run an HTTP server from its workspace and expose it at a stable URL such as:

```text
https://[assistant-id].assistant.larryhudson.io
```

The assistant can then send links to that surface in email, and the user can use it for dashboards, approvals, forms, review flows, reports, task views, or direct API calls. Email remains the primary interface. The surface is an optional escalation path when email is not enough.

## Current Alignment

The implementation plan has since narrowed this brainstorm into a personal-first v1:

- Use path-based routing first: `/surfaces/{assistant_id}/...`.
- Reuse existing admin Basic Auth for browser access.
- Use simple static bearer tokens for API clients such as Apple Shortcuts.
- Treat surfaces as assistant-run HTTP servers on port `8000`; static files can be served by the assistant server.
- Use synthetic inbound email messages for v1 surface-triggered agent runs.
- Make the Assistant Tools API an HTTP + OpenAPI contract, callable with `httpx`, `curl`, JS `fetch`, or generated clients.
- Let the assistant self-test its surface locally via `curl http://localhost:8000/` before emailing links.
- Defer wildcard subdomains, manifests, scale-to-zero, route scopes, rich audit tables, and generalized run triggers.

For implementation handoff, treat `docs/assistant-surfaces-implementation-plan.md` as the source of truth. This synthesis remains useful for product framing and later design options.

## Core Framing

The strongest shared framing from the agents was:

> Assistant-owned surface, platform-owned edge.

The assistant should be free to decide what UI best fits its work. A budget assistant might build charts and approval flows. A writing assistant might build a rich editor. A research assistant might build a source review dashboard. The core app should not need to know the semantics of budgets, drafts, reports, invoices, or projects.

But the assistant should not own the dangerous parts:

- Public routing
- User authentication
- Permission checks
- Email sending
- Agent-run creation
- Approval recording
- Secret access
- Budget enforcement
- Audit logs
- Cross-assistant isolation
- Resource limits

Those stay platform-owned.

## Platform Responsibilities

The platform becomes a small runtime and control plane for assistant-authored apps.

It should own:

- **Routing**: map assistant paths or subdomains to the correct workspace surface.
- **TLS and public edge**: terminate HTTPS and normalize inbound requests.
- **Auth**: validate user sessions, email magic links, scoped tokens, and capability links.
- **Identity injection**: pass trusted identity to the assistant through headers or a local bridge.
- **Lifecycle**: start, stop, warm, suspend, restart, archive, and health-check assistant surfaces.
- **Sandboxing**: keep assistant code isolated from the platform and from other assistants.
- **Assistant Tools API**: provide typed APIs for privileged operations.
- **Audit**: record requests, tool calls, approvals, run triggers, email sends, and failures.
- **Limits**: enforce CPU, memory, disk, egress, request rate, cost, and runtime limits.
- **Rollback**: allow reverting to a prior published surface if the assistant breaks the UI.

The platform should not own:

- Assistant-specific page structure.
- Assistant-specific task schemas.
- Assistant-specific dashboards.
- Assistant-specific frontend frameworks.
- The domain-specific workflow model unless it is truly cross-cutting.

## Assistant Responsibilities

The assistant can own:

- HTML, CSS, JS, or server-side templates.
- A local HTTP server, static site, or lightweight framework.
- Domain-specific state in workspace files or SQLite.
- Local pages and routes.
- UI copy and interaction flow.
- Local caching and projections.
- Domain-specific forms, charts, tables, editors, and review screens.

The assistant should call the Assistant Tools API for anything with external consequences.

For example, a button in an assistant dashboard should not directly send email or mutate platform tables. It should call a platform-mediated operation such as:

```text
runs.create(...)
email.send(...)
approvals.create(...)
approvals.resolve(...)
events.log(...)
secrets.get(...)
```

## Promising Architecture

The most promising direction is a **tunneled workspace server**.

In this model:

1. The assistant workspace contains an app, for example `app.py`, `server.ts`, or `public/index.html`.
2. The assistant starts a local HTTP server inside the workspace or sandbox.
3. The server binds only to an internal port, such as `localhost:8000`.
4. The platform reverse-proxies `https://[assistant-id].assistant.larryhudson.io` to that internal port.
5. The platform validates auth before proxying.
6. The platform injects trusted request context, such as user id, assistant id, scopes, and request id.
7. The assistant renders the UI and calls the Assistant Tools API for privileged actions.

This keeps the assistant flexible while keeping the platform boundary clear.

### Why This Shape Is Attractive

- It is natural for agents: run a small web server, generate files, iterate.
- It is framework-agnostic: Flask, FastAPI, Next.js, htmx, Streamlit-style wrappers, or static HTML can all fit.
- It does not require the core app to model every workflow.
- It allows assistants to evolve their UX without platform changes.
- It has a clean security boundary: HTTP in, typed assistant tools out.
- It fits the existing workspace mental model.

### Main Cost

The platform becomes responsible for a small hosting/runtime layer:

- Process lifecycle
- Port discovery
- Reverse proxying
- Cold starts
- Logs
- Resource limits
- Sandboxed networking
- Health checks

This is heavier than serving generated static HTML, but much lighter than building a fixed app-owned collaboration product.

## Alternative Designs

### Static Published Site

The assistant writes files to a `public/` directory. The platform serves them at the assistant URL.

This is the simplest option:

- No long-running server.
- Cheap to host.
- Easy to snapshot and roll back.
- Strong safety properties.

The limitation is interactivity. Forms and buttons would need to post directly to platform APIs or to generated action links.

This could be a good first slice or fallback mode.

### Heroku for Assistants

Each assistant workspace behaves like a tiny deployable app:

- Manifest or Procfile declares the web command.
- Build step installs dependencies.
- Platform deploys and proxies the app.

This is flexible and familiar, but introduces more deployment complexity. It may be a later version of the tunneled workspace server idea.

### Streamlit or Gradio Style UI

The platform could provide a declarative UI library:

```python
ui.title("Budget dashboard")
ui.button("Approve", action="approve_budget")
ui.table(transactions)
```

This is easy for agents to generate, but it couples assistant UX to a platform framework. It may be useful as an optional helper, not the main architecture.

### Hybrid Static First, Dynamic Later

A pragmatic path is:

1. Static generated surface.
2. Static surface plus platform action bridge.
3. htmx or server-rendered dynamic app.
4. Full assistant-hosted web server when needed.

Assistants can graduate from static to dynamic as complexity grows.

## Assistant Tools Model

The platform should expose a narrow set of typed assistant tools. These tools are the privileged effect surface.

Likely tools:

- `runs.create`: queue an agent run from a surface action.
- `runs.get`: check run status.
- `runs.stream`: stream logs or progress for a run.
- `email.send`: send an email through the platform.
- `email.draft`: create a draft or proposed email.
- `email.get_thread`: read scoped email thread context.
- `approvals.create`: request human approval.
- `approvals.resolve`: record approval or rejection.
- `actions.create_link`: create a scoped magic/action link.
- `events.log`: append an audit/event record.
- `state.get` / `state.put`: access platform-mediated state when needed.
- `artifacts.publish`: publish generated files or reports.
- `secrets.get`: retrieve scoped secrets.
- `budget.check`: check whether an operation is allowed.
- `schedule.upsert`: schedule future or recurring work.

Each tool should have:

- A schema.
- A risk class.
- Permission requirements.
- Audit behavior.
- Idempotency support where relevant.
- Rate and budget limits.

This tool list may be the most important design artifact before implementation.

## Run Triggering From Surfaces

Surface-triggered agent runs should use the same run pipeline as inbound email.

The run source changes, but the system should still produce a normal `AgentRun`:

```text
source = "assistant_surface"
assistant_id = ...
user_id = ...
action_id = ...
request_id = ...
input = ...
```

A typical flow:

1. User clicks "Re-analyze May spending" in the assistant surface.
2. Assistant route validates the request context.
3. Assistant calls `runs.create(...)`.
4. Platform queues the run asynchronously.
5. Frontend shows pending state and polls or streams progress.
6. Agent run updates workspace state or published artifacts.
7. Frontend refreshes.
8. Assistant may also send an email summary.

The surface should not block on long model work inside the HTTP request. Agent runs should be async by default.

## Auth and Capability Links

Email-first products need auth that works naturally from email.

Likely auth modes:

- Logged-in owner session.
- Magic links sent by email.
- Scoped capability links for specific actions.
- Short-lived tokens embedded in email links.

The platform should validate these before traffic reaches the assistant. The assistant can receive trusted context through headers such as:

```text
X-Assistant-Id: budget-bot
X-User-Id: user@example.com
X-Scopes: read:dashboard approve:budget
X-Request-Id: ...
```

The assistant should not be able to mint arbitrary authority. It can request capability links from the platform, but the platform signs them, scopes them, expires them, and audits their use.

Sensitive actions should use one-time nonces or explicit approval records.

## State Model

A useful split:

### Assistant-Owned State

Stored in the workspace:

- SQLite databases
- JSON files
- Generated HTML
- Cached API results
- Domain-specific app state
- Frontend code

This state is flexible and assistant-shaped.

### Platform-Owned State

Stored in the app database:

- Assistants
- Email threads and messages
- Agent runs and run steps
- Usage and budgets
- Approval records
- Action links and tokens
- Schedules
- Audit events
- Published surface versions
- Process/runtime status

This state is durable, inspectable, and security-relevant.

### Optional Published Facts

One concern with assistant-owned state is discoverability. The platform may need a lightweight way for assistants to publish selected facts without imposing a full schema.

For example:

```text
assistant_published_facts(
  assistant_id,
  kind,
  key,
  summary,
  url,
  updated_at
)
```

This gives the platform some visibility while preserving assistant flexibility.

## Lifecycle

A surface could move through states:

```text
disabled -> static -> starting -> running -> idle -> stopped -> failed -> archived
```

Important lifecycle behaviors:

- Wake on first request.
- Keep warm for a short period after traffic, such as 30 minutes.
- Health-check before proxying.
- Show a platform loading or unavailable page during startup/failure.
- Persist workspace files across restarts.
- Record deployed surface version or content hash.
- Support rollback to a prior version.
- Archive inactive assistants.
- Return a clear archived/reactivation page for old email links.

## Audit and Observability

The platform should log:

- Surface deployments and version changes.
- HTTP requests to assistant surfaces.
- Auth checks and token validations.
- Assistant Tools API calls.
- Agent runs started from surface actions.
- Approval requests and resolutions.
- Emails sent from surface-triggered flows.
- Secret access.
- Denied permissions.
- Crashes, restarts, cold starts, and timeouts.

The core audit question should be answerable:

> What did the user click, what did the assistant do, what assistant tools were called, what external effects happened, and which agent run produced them?

## Safety Boundaries

Major risks:

- Assistant-generated surface is broken, misleading, or unsafe.
- XSS or token leakage from generated pages.
- CSRF or replay attacks on action links.
- Assistant server SSRF or uncontrolled egress.
- Assistant spams platform APIs.
- User is confused by custom assistant UIs.
- Surface server and agent run write conflicting state.
- Cold starts make email links feel broken.
- A compromised assistant tries to access other assistants or platform internals.

Mitigations:

- Assistant server binds only inside sandbox.
- Platform validates auth before proxying.
- Short-lived, scoped, one-time tokens for sensitive operations.
- Strict CSP and cookie handling.
- No platform database credentials in the assistant workspace.
- Assistant Tools API access scoped to one assistant.
- Rate limits and budgets on every tool.
- Resource limits on surface process.
- Egress proxy or deny-by-default network policy.
- Audit every privileged call.
- Platform-owned header/footer or origin indicator for user trust.
- User-visible "disable surface" or "email-only mode".
- Rollback and kill switch.

## First Slice

A concrete first slice could be:

1. Add an assistant surface URL.
2. Let the assistant run an HTTP server on port `8000`.
3. Proxy path-based surface requests through the platform with Basic Auth.
4. Add a small Assistant Tools API for `runs.create`, `runs.get`, and `events.log`.
5. Let the assistant include surface links in email replies.
6. Log surface requests and tool calls.

Then evolve to:

1. Add static bearer tokens for API clients.
2. Add signed expiring links if non-owner sharing becomes useful.
3. Add manifest-based commands/ports if port `8000` becomes too limiting.
4. Add generalized run triggers if synthetic inbound email becomes awkward.
5. Surface-triggered runs remain queued asynchronously.

## Most Promising Product Direction

The product is not a fixed collaboration app. It is closer to:

> A runtime for small AI-authored apps that speak email.

Email remains the durable conversation and notification surface. The assistant-owned surface becomes a contextual work surface that the assistant can create, revise, and link to when email is not enough.

This direction preserves assistant flexibility while keeping the central app light. The hard part is designing the platform edge and Assistant Tools API carefully enough that assistant-authored surfaces are useful without becoming unsafe or ungoverned.
