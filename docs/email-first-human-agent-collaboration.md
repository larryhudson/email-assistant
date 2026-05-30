# Email-First Human-Agent Collaboration Ideas

This document summarizes brainstorming around richer collaboration for `email-assistant`: an assistant whose primary interface is email. The user emails the assistant, the system runs an agent session in a workspace folder, and the assistant replies by email.

The immediate design question was how to improve on the current pattern where the agent keeps its own SQLite database for tasks/state. That works well for agent flexibility, but the human has poor visibility into what the agent knows, plans, or is waiting on.

## Current Shape

- Email is the primary command, notification, and response surface.
- The app already persists email threads/messages, agent runs, run steps, prompts, usage, scheduled tasks, and read-only admin inspection views.
- The agent may maintain workspace files or SQLite databases as working state.
- The missing piece is a human-visible collaboration layer: a source of truth that both the agent and human can inspect and update.

## Core Tension

There are three potential stores of truth:

1. **Email threads**: what the human actually sees and trusts.
2. **Platform database**: durable operational state, run history, audit, admin visibility.
3. **Agent workspace**: files, SQLite, generated HTML, and other flexible working materials.

Agent-owned SQLite is useful because it gives the agent freedom to evolve its schema per task. It is weak as a collaboration source of truth because it is not naturally visible, governed, audited, or connected to the product UI.

The central design question is: **what should be canonical, what should be a projection, and when should the user leave email for a richer surface?**

## Strongest Shared Direction

The strongest convergence from the brainstorm was:

> Keep email primary, but add a platform-owned collaboration kernel for shared state. Let agents generate flexible views over that state, while the app owns permissions, actions, audit history, and external side effects.

In practice:

- Email starts work, carries summaries, and handles small decisions.
- A shared collaboration kernel stores tasks, questions, decisions, drafts, approvals, artifacts, schedules, actions, and events.
- Web views are optional and contextual: useful for scanning, editing, approving, or reviewing larger state.
- Agent-generated dashboards are presentation/projection layers, not arbitrary authority.
- Actions are typed, permissioned, idempotent where possible, and audited.

## Collaboration Kernel

A collaboration kernel is a small, explicit shared state model that both agent and human can read and write through controlled interfaces.

Possible primitives:

- **Workstream / case**: a durable container that can span multiple email threads.
- **Task**: title, status, owner, priority, due date, blockers.
- **Question**: something the agent needs from the human.
- **Decision**: what was decided, by whom, when, and why.
- **Draft**: proposed email, document, plan, state change, or other item awaiting review.
- **Artifact**: file, report, generated page, document, link, or rendered output.
- **Proposal**: an agent-suggested mutation or external action awaiting commit.
- **Approval**: a human decision on a proposal or risky action.
- **Action definition**: a registered executable capability with schema, risk class, and policy.
- **Action run**: one execution attempt, linked to a run, proposal, approval, and result.
- **Schedule**: delayed or recurring work.
- **Event**: append-only audit history of meaningful changes.
- **Source/provenance**: email, file, URL, prior run, or human edit that supports a fact.

Important separation:

- Agent scratch state can stay flexible, private, and disposable.
- Collaboration state should be structured, durable, inspectable, and stable.

## Email As Control Plane

Several agents framed email as the **control plane**, not the full data plane.

Email should be good at:

- Starting work.
- Giving instructions.
- Asking and answering small questions.
- Approving or rejecting proposed actions.
- Receiving summaries, digests, and status updates.
- Linking to richer views when needed.

Email should not try to hold:

- Large task boards.
- Complex relational state.
- Long audit trails.
- Multi-step forms.
- Dense review interfaces.

This suggests an escalation ladder:

1. Simple email reply.
2. Structured reply: `APPROVE`, `REJECT`, `1`, `2`, or edited quoted text.
3. Signed/magic link for a focused action or review.
4. Temporary dashboard for a specific task or workstream.
5. Standing portal/home for long-lived assistants or power users.

## Reply-As-UI

For many workflows, email replies may be enough:

- `APPROVE`
- `REJECT`
- `Option B`
- `Change the deadline to Friday`
- `Yes, but make the budget GBP 500`
- Editing quoted task lists and replying
- Forwarding an email to the assistant with instructions

The platform can parse these replies into typed kernel events.

Tradeoffs:

- Very low friction and preserves the email-first experience.
- Works well for simple approvals, choices, corrections, and follow-ups.
- Becomes fragile for bulk editing, browsing large state, or resolving ambiguity.

## Magic Links And Guest Views

For richer interactions, the assistant can email scoped links:

- "Review these tasks"
- "Approve this quote"
- "Answer 3 open questions"
- "Edit the draft"
- "View the project dashboard"

Useful properties:

- Capability-scoped: the link only grants access to one action or workstream.
- Short-lived for sensitive actions.
- Bound to assistant/user/sender context where possible.
- Idempotent: clicking twice should not duplicate effects.
- Audited: every click/action records who, what, when, and which email/run it came from.

This is especially useful for users who should not need a full account or dashboard login.

## Agent-Generated Dashboards

The user idea of agent-generated HTML dashboards was strongly supported, with a major constraint:

> The agent may improvise views, but it should not invent invisible authority.

Good version:

- Agent generates static or declarative HTML/Markdown views.
- Views are scoped to a run, workstream, or assistant.
- Views read from the collaboration kernel or from safe rendered snapshots.
- Buttons/forms map to registered action IDs.
- The app executes actions through trusted handlers.
- All actions are logged and permissioned.

Risky version:

- Agent generates arbitrary JavaScript with direct database or backend access.
- Buttons run arbitrary commands.
- Generated pages mutate hidden state without audit.
- The user cannot tell what an action will really do.

The promising architecture is: **agent-shaped presentation, platform-owned execution**.

## Propose Then Commit

A recurring pattern was Git-like semantics for important changes.

The agent can propose:

- Add or update tasks.
- Change memory.
- Send an email.
- Schedule a recurring task.
- Edit a file.
- Call an external service.
- Spend budget.

The human can commit, reject, or revise the proposal by email or web action.

This gives:

- Clear audit trails.
- Human control over risky changes.
- A natural way to review diffs.
- Space for auto-commit policies on low-risk changes.

Tradeoff:

- Too much approval creates friction.
- The system needs risk tiers and user-configurable autonomy policies.

## Safety Boundaries

Recommended boundaries:

- **Risk tiers**: read, write collaboration state, external write, irreversible/destructive.
- **Capability URLs**: scoped, expiring, single-purpose links.
- **Typed action registry**: actions have schemas, policies, and handlers.
- **Approval policies**: auto/ask/forbid per action class, assistant, user, or workstream.
- **Idempotency keys**: repeated approvals or retries should be safe.
- **Budget integration**: web-triggered agent runs consume the same budget ledger as email-triggered runs.
- **Explainability**: every action links back to proposal text, approving message/action, run ID, and before/after diff where relevant.
- **Kill switches**: pause per assistant or workstream.
- **Sandbox boundary**: agent workspace remains sandboxed; kernel mutations and external actions go through platform APIs/tools, not arbitrary raw access.

## Architecture Options

### Option A: Platform-Owned Kernel

Store collaboration state in the platform database, likely Postgres, and expose it to the agent through tools or a `CollaborationPort`.

Pros:

- Single source of truth.
- Strong audit and visibility.
- Fits existing database/admin architecture.
- Human and agent use the same state.
- Easier to build stable UI and email links.

Cons:

- Less schema freedom for the agent.
- Requires designing product-level APIs/tools.
- Agent may still need private scratch state for flexible reasoning.

This was the most strongly recommended backbone.

### Option B: Agent SQLite Plus Projection

Let the agent keep SQLite, then mirror selected tables/entities into platform state after each run or continuously.

Pros:

- Preserves agent flexibility.
- Allows incremental adoption.
- The agent can still improvise schema quickly.

Cons:

- Two sources of truth.
- Sync conflicts and drift.
- Harder real-time audit.
- The product may never know what matters unless the agent publishes it.

This is useful as a transition, but weaker as a long-term model.

### Option C: Shared SQLite Kernel

Use a shared SQLite database as the kernel, possibly with WAL mode and web views over it.

Pros:

- Simple local architecture.
- Agent can use familiar SQL directly.
- Good for single-user/local assistant setups.

Cons:

- Harder multi-tenant story.
- Direct DB mutation can bypass governance.
- Container/shared volume complexity.
- Weaker fit with the app's existing Postgres operational model.

This may be good for experiments, but should probably not be the durable product center.

### Option D: Event-Sourced Kernel

Everything important becomes an append-only event; current task/question/decision views are projections.

Pros:

- Excellent auditability.
- Fits agent provenance and replay/debugging.
- Generated dashboards and digests become regenerable read models.

Cons:

- More implementation complexity.
- Requires careful event schema design and migration.

This is attractive if audit/provenance becomes central, but may be heavier than needed initially.

### Option E: Email-Only Structured Payloads

Agent embeds structured blocks in email, such as Markdown tables or YAML/JSON snippets. The human edits/replies, and the platform parses the response into the kernel.

Pros:

- Pure email-primary.
- No new UI required.
- Good for technical/power users.

Cons:

- Awkward for non-technical users.
- Fragile parsing.
- Poor for large or visual state.

This can complement other options, but probably should not be the only interaction model.

## UX Surfaces

Possible surfaces, ordered from least to most product weight:

- Plain email summaries.
- Email summaries with stable links.
- One-click signed action links.
- Structured reply parsing.
- HTML email cards/snapshots.
- Magic-link focused pages.
- Per-workstream dashboard.
- Per-assistant portal/home.
- Admin/operator UI.
- Agent-generated dashboard views.

The useful principle is progressive disclosure: most work stays in email, richer surfaces appear only when the user needs them.

## Distinct Ideas From The Agent Brainstorm

Claude emphasized:

- Email as conversation log, DB as working memory, UI as temporary windows into the memory.
- Stable URLs for every task, decision, draft, and artifact.
- Drafts as a first-class type.
- Watches/subscriptions: "ping me if X changes."
- Confidence/provenance on facts.
- Push email-native interaction further before building too much UI.

Codex emphasized:

- The product question is when the human needs structured visibility or control beyond an email reply.
- Generated views over governed state.
- Clear separation of agent memory from collaboration state.
- Action capabilities, not action code.
- A `CollaborationPort` fits naturally with the current architecture.

Antigravity emphasized:

- "Email-first, web-occasional."
- Ephemeral web mirrors through secure magic links.
- IMAP drafts-folder handoff for email-native draft review.
- Interactive HTML email snapshots, with the caveat that email clients vary.
- An "action firewall" where the platform executes high-risk actions only after approval.

Copilot emphasized:

- Embedded approvals and read-only dashboards as a low-risk first step.
- Email buttons/forms where client support allows.
- Open questions around authentication, concurrency, and retention.
- Starting with read-only visibility before allowing direct human edits.

Cursor emphasized:

- The drift between email threads, platform Postgres, and agent workspace.
- Email as control plane, not data plane.
- Progressive disclosure from reply to magic link to dashboard.
- Reply-as-UI.
- Workstreams/cases because email threads are weak project boundaries.
- Propose/commit semantics.
- Attention state: blocked on human, informational, stale, or no action needed.

## Suggested Near-Term Direction

A pragmatic path:

1. Define a small platform-owned collaboration kernel: workstreams, items, proposals, approvals, artifacts, events, schedules.
2. Expose it to the agent through tools, not raw database access.
3. Add email footer links for relevant entities and open questions.
4. Support simple reply parsing for approvals and choices.
5. Add magic-link pages for focused review/edit flows.
6. Add generated dashboards later as declarative views over the kernel.
7. Keep agent SQLite/files as private scratch space unless the agent explicitly publishes state into the kernel.

This preserves email as the primary interface while giving the human visibility and control when the work becomes stateful, risky, or too large for email.

## Things To Avoid Initially

- A generic DB browser as the main user experience.
- Making agent-owned SQLite the canonical shared state.
- Letting generated dashboards execute arbitrary code/actions.
- Building a full portal before validating email-native approvals and magic links.
- Real-time collaborative editing unless a concrete workflow demands it.
- Over-modeling every possible workflow before the smallest useful kernel exists.

## Open Questions

- Which collaboration primitives are truly needed for the first useful version?
- Should the kernel be CRUD tables, event-sourced, or CRUD with an append-only event log?
- How much should humans edit directly versus approve/reject agent proposals?
- How should capability links authenticate users and expire?
- What is the conflict model when a human edits while an agent run is active?
- Which actions can be auto-committed, and which require approval?
- How should agent private scratch state be surfaced or ignored?
- What belongs in per-thread state versus a broader workstream?
- How should summaries/digests avoid email fatigue?
