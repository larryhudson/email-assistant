# Email Agent MVP Handoff

## Context

Build a Python backend for an email-based AI assistant. The first deployment is for one non-technical end user, with a separate technical admin/operator:

- Admin/operator: Larry
- End user: Larry's mum
- End-user interface: email only
- Admin interface: web UI for inspection, cost tracking, pause/approval controls, and debugging
- Agent framework: PydanticAI
- First email provider adapter: Mailgun
- Memory layer: behind a port; adapter can be Cognee or a minimal local implementation

The system should be single-stack and multi-assistant capable. Larry's mum's assistant is the first assistant. Larry should be able to add his own assistant later without deploying a second stack.

## MVP Goals

1. Receive inbound emails via Mailgun webhook.
2. Route each inbound email to exactly one assistant using a separate inbound address per assistant.
3. Maintain isolated thread history, memory, tools, budget, and run logs per assistant.
4. Run a PydanticAI agent non-interactively.
5. Send an email reply through the email provider adapter.
6. Track cost and enforce a monthly budget per assistant.
7. Provide an admin-facing run ledger sufficient to inspect what happened.
8. Keep email provider and memory provider swappable via ports.

## Non-Goals For MVP

- Full multi-admin permissions model.
- Complex CRM-style user management.
- Sophisticated memory ranking beyond basic thread history plus semantic recall.
- Calendar/payment/high-risk tool integrations unless explicitly added later.
- Letting the end user use a web UI.

## Core Architecture

Runtime flow:

1. Receive provider webhook.
2. Verify provider signature.
3. Normalize inbound email.
4. Resolve assistant by inbound address.
5. Check assistant budget.
6. Resolve or create email thread.
7. Recall memory inside assistant scope.
8. Run PydanticAI agent.
9. Build outbound reply envelope.
10. Send reply through email provider adapter.
11. Record run, messages, trace, usage, and memory jobs.
12. Run durable memory extraction asynchronously.

Critical path should stop before heavy enrichment. Summarization, embeddings, and memory curation happen after the reply is sent.

## Ports

### Email Provider Port

Core runtime must not depend directly on Mailgun payloads or send parameters.

```python
class EmailProvider(Protocol):
    async def verify_webhook(self, request: WebhookRequest) -> None: ...
    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail: ...
    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail: ...
```

`NormalizedInboundEmail` must preserve:

- provider message ID
- `Message-ID`
- `In-Reply-To`
- `References`
- from address
- to/recipient addresses
- subject
- plain text body
- HTML body if needed
- attachments metadata
- received timestamp

First adapter: `MailgunEmailProvider`.

### Memory Port

Core runtime must not depend directly on Cognee, pgvector, or local schema details.

```python
class MemoryPort(Protocol):
    async def recall(
        self,
        assistant_id: str,
        thread_id: str,
        query: str,
    ) -> MemoryContext: ...

    async def record_run(
        self,
        assistant_id: str,
        run: CompletedRun,
    ) -> None: ...

    async def curate_after_run(
        self,
        assistant_id: str,
        thread_id: str,
        run_id: str,
    ) -> None: ...

    async def delete_assistant_memory(self, assistant_id: str) -> None: ...
```

Candidate adapters:

- `CogneeMemoryAdapter`
- `LocalMemoryAdapter`
- `InMemoryMemoryAdapter` for tests

Invariant: every memory operation receives `assistant_id` and must not cross assistant scope.

## Key Modules

### Assistant Router

Input: normalized inbound email.

Output: assistant run scope.

Responsibilities:

- Resolve assistant by inbound address.
- Load assistant config, end user, owner/admin, budget, memory namespace, and tool allowlist.
- Reject paused/disabled assistants.

### Budget Governor

Input: assistant ID, current usage ledger, estimated run cost.

Output:

- allow
- require approval
- degrade
- budget-limit reply

Budget limit behavior for MVP:

Send a cheap template reply, without another model call:

> I’ve hit my budget limit for this month; it resets in X days.

### Thread Resolver

Input: normalized inbound email and assistant scope.

Output: internal thread.

Resolution order:

1. Match provider thread/conversation ID if available.
2. Match `In-Reply-To` against indexed message IDs for this assistant.
3. Match `References` headers against indexed message IDs for this assistant.
4. Create a new thread.

Important: index both inbound and outbound `Message-ID` values with `assistant_id`.

### Reply Envelope Builder

Input: inbound email, thread, agent reply text.

Output: normalized outbound email.

Responsibilities:

- Preserve email threading headers.
- Generate outbound `Message-ID`.
- Set `In-Reply-To` to inbound `Message-ID`.
- Build `References` from inbound references plus inbound `Message-ID`.
- Use `Re:` subject when needed.

### Run Recorder

Input: completed run.

Responsibilities:

- Store inbound message.
- Store outbound message.
- Store agent run status.
- Store run steps/tool summaries.
- Store usage/cost.
- Enqueue memory curation.
- Handle retries/idempotency.

## PydanticAI Agent

Use PydanticAI as the agent orchestration layer.

Agent input should include:

- normalized inbound email
- assistant profile/config
- recent thread window
- memory context
- available tools
- budget/safety constraints

The agent should have tools for:

- inspecting full thread history on demand
- inspecting durable memory on demand
- possibly asking for admin approval later

Do not inject full thread history into every prompt by default. Store full history and make it inspectable. Start prompt context with a recent window plus summarized memory.

## Minimal Data Model

Suggested tables:

```text
owners
  id
  name
  primary_admin_id
  billing_scope

admins
  id
  owner_id
  email
  role

end_users
  id
  owner_id
  email
  display_name

assistants
  id
  end_user_id
  inbound_address
  policy_id
  status

assistant_scopes
  assistant_id
  memory_namespace
  tool_allowlist
  budget_id

email_threads
  id
  assistant_id
  end_user_id
  root_message_id
  subject_normalized
  created_at
  updated_at

email_messages
  id
  thread_id
  assistant_id
  direction
  provider_message_id
  message_id_header
  in_reply_to_header
  references_headers
  from_email
  to_emails
  subject
  body_text
  body_html
  created_at

message_index
  assistant_id
  message_id_header
  thread_id
  provider_message_id

agent_runs
  id
  assistant_id
  thread_id
  inbound_message_id
  reply_message_id
  status
  error
  started_at
  completed_at

run_steps
  id
  run_id
  kind
  input_summary
  output_summary
  cost_cents
  created_at

usage_ledger
  id
  assistant_id
  run_id
  provider
  model
  input_tokens
  output_tokens
  cost_cents
  budget_period
  created_at

budgets
  id
  assistant_id
  monthly_limit_cents
  period_starts_at
  period_resets_at

memories
  id
  assistant_id
  source_thread_id
  source_run_id
  text
  embedding
  metadata
  importance
  created_at
```

For the MVP, this can be simplified if using Cognee for memory, but keep the app-level `agent_runs`, `usage_ledger`, `email_threads`, and `email_messages` tables regardless.

## Admin Interface MVP

Minimum views:

1. Assistants
   - status
   - inbound address
   - monthly budget
   - spend this period
   - pause/resume

2. Runs
   - inbound email
   - retrieved context summary
   - tools called
   - outbound reply
   - status/errors
   - cost

3. Threads
   - full email history
   - message IDs/threading headers
   - related runs

4. Memory
   - durable memories by assistant
   - delete/demote memory
   - source thread/run links

5. Budget
   - monthly limit
   - reset date
   - threshold alerts
   - cost breakdown

## Cost Controls

MVP behavior:

- Check budget before model/tool calls.
- Use estimated run cost before the run.
- Write actual usage after the run.
- Stop hard when the budget is reached.
- Send a template budget-limit reply.
- Notify admin when thresholds are crossed.

Track at least:

- model provider
- model name
- input tokens
- output tokens
- estimated cost
- actual cost where available
- memory extraction/embedding cost
- tool/API cost if applicable

## Memory Strategy

First version:

- Full thread history is stored and inspectable.
- Prompt gets a recent thread window.
- Agent can inspect older thread messages via tool.
- Durable memory capture starts permissive.
- Admin can delete or demote bad memories.
- Tighten memory extraction policy after observing real runs.

Durable memory candidates:

- preferences
- decisions
- recurring contacts
- unresolved tasks
- commitments
- important facts about ongoing situations

## Testing Strategy

Test through ports and deep modules, not provider internals.

Priority tests:

- Mailgun webhook normalizes to `NormalizedInboundEmail`.
- Assistant router maps inbound address to the correct assistant.
- Budget governor returns budget-limit reply decision when limit is reached.
- Thread resolver matches replies using outbound assistant `Message-ID`.
- Thread resolver does not cross assistant scope.
- Reply envelope preserves `In-Reply-To` and `References`.
- Run recorder is idempotent for duplicate webhook delivery.
- Memory port never returns memories from another assistant.
- PydanticAI agent run can be tested with fake email and fake memory adapters.

## Suggested Implementation Slices

### Slice 1: Core Data + Ports

- Define normalized email models.
- Define email provider port.
- Define memory port.
- Add database schema for owners/admins/end users/assistants/threads/messages/runs/budgets.
- Add in-memory adapters for tests.

### Slice 2: Mailgun Inbound + Threading

- Implement Mailgun webhook verification.
- Implement Mailgun inbound parser.
- Implement assistant router by inbound address.
- Implement thread resolver and message index.
- Store inbound messages.

### Slice 3: Budget + Template Replies

- Implement budget governor.
- Implement usage ledger skeleton.
- Implement budget-limit template reply.
- Implement Mailgun outbound send adapter.

### Slice 4: PydanticAI Agent Runtime

- Implement agent run orchestration.
- Add recent-thread context.
- Add thread-inspection tool.
- Add memory recall tool through memory port.
- Store outbound reply and run trace.

### Slice 5: Memory Adapter

- Choose initial adapter: Cognee or local.
- Implement `recall`, `record_run`, and `curate_after_run`.
- Add background memory curation job.
- Add admin memory inspection.

### Slice 6: Admin UI

- Assistant list and pause/resume.
- Run ledger.
- Thread viewer.
- Cost dashboard.
- Memory viewer/delete/demote.

## Open Decisions

- Initial memory adapter: Cognee or local.
- Database choice.
- Web framework.
- Queue/background job mechanism.
- Exact budget defaults for first assistant.
- First set of tools available to the assistant.

