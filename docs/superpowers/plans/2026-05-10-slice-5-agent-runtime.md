# Slice 5 — PydanticAI Agent Runtime Implementation Plan

**Goal:** Make `AssistantRuntime.execute_run(run_id)` work end-to-end. After this slice, an inbound email queued by `accept_inbound` can be turned into a recorded run with a sent reply, using in-memory adapters for the unit suite and the real Mailgun + Docker + (placeholder) memory adapters in production.

**Architecture:** Five new pieces.

1. **`agent/assistant_agent.py`** — `AssistantAgent` wraps a PydanticAI `Agent[AgentDeps, str]`. Per-run state flows through `RunContext[AgentDeps]`. Six tools: `read`, `write`, `edit`, `bash` (route to `ctx.deps.sandbox`); `memory_search` (calls `ctx.deps.memory.search` directly — bytes never enter the container); `attach_file` (appends to `ctx.deps.pending_attachments`).
2. **`domain/reply_envelope.py`** — `ReplyEnvelopeBuilder.build(inbound, thread, body, attachments) -> NormalizedOutboundEmail`. Owns subject `Re:` logic + `In-Reply-To`/`References` chaining. The slice-3 `build_budget_limit_reply` gets refactored to call this so both reply paths share.
3. **`domain/run_recorder.py`** — `RunRecorder.record_completion(scope, run_id, outbound, steps, usage)`: single transaction writes `email_messages(direction='outbound')` + `message_index` + updates `agent_runs(status, completed_at, reply_message_id)` + writes `run_steps` + `usage_ledger`. Idempotent on `(assistant_id, provider_message_id)` via the unique constraint already on `email_messages`. Enqueues `curate_memory(...)` — for slice 5 that's a no-op stub returning the planned job spec; the real Procrastinate enqueue lands in slice 7.
4. **Updated `AssistantRuntime`** — `accept_inbound` now also writes an `agent_runs(status="queued")` row alongside the inbound message (idempotent), and `execute_run(run_id)` is the new worker entry: load run → `BudgetGovernor.decide` → projector → sandbox setup → memory recall → `agent.run()` → read attachment bytes → build envelope → `EmailProvider.send_reply` → `RunRecorder.record_completion`. Per-run wall-clock timeout. Errors mark the run `failed` with the exception message.
5. **`models/agent.py`** — `AgentDeps` (frozen-by-convention dataclass with the mutable `pending_attachments: list[PendingAttachment]`), `RunUsage` (token counts + cost cents), `RunStep` (kind/input_summary/output_summary/cost_cents).

**Tech additions:**
- `pydantic-ai-slim` (`>= 0.0.x`) — full `pydantic-ai` is heavier; slim has the model+tool plumbing we need.
- `pydantic-ai`'s `TestModel` and `FunctionModel` for unit tests — sub-second, deterministic, no API key.

**Out of scope (later slices):** real Cognee adapter (slice 6 — we use `InMemoryMemoryAdapter` here), Procrastinate worker dispatch (slice 7 — `accept_inbound` writes the row but doesn't enqueue; `execute_run` is invoked by tests directly), idle-shutdown sweeper, allowlist egress, admin UI.

**Out of scope for this slice but smoke-tested behind a flag:** real DeepSeek call via the OpenAI-compatible provider — gated by `EMAIL_AGENT_E2E=1` so CI doesn't pay for tokens.

---

## File Structure

**Create:**
- `src/email_agent/models/agent.py` — `AgentDeps`, `RunUsage`, `RunStepRecord`.
- `src/email_agent/agent/__init__.py` — package marker.
- `src/email_agent/agent/assistant_agent.py` — `AssistantAgent`, tool definitions.
- `src/email_agent/domain/reply_envelope.py` — `ReplyEnvelopeBuilder`.
- `src/email_agent/domain/run_recorder.py` — `RunRecorder`, `CompletedRun` input model.
- `tests/unit/agent/__init__.py`
- `tests/unit/agent/test_assistant_agent.py` — `TestModel` / `FunctionModel`-driven tool dispatch tests.
- `tests/unit/domain/test_reply_envelope.py`
- `tests/unit/domain/test_run_recorder.py`
- `tests/unit/runtime/test_execute_run.py` — end-to-end with all in-memory adapters + scripted `FunctionModel`.
- `tests/integration/test_deepseek_smoke.py` — gated by `EMAIL_AGENT_E2E=1`, hits DeepSeek for one tiny prompt.

**Modify:**
- `src/email_agent/domain/budget_reply.py` — refactor to call `ReplyEnvelopeBuilder.build` so threading + subject logic lives in one place.
- `src/email_agent/runtime/assistant_runtime.py` — `accept_inbound` writes `agent_runs(status="queued")`; new `execute_run(run_id)` orchestrator.
- `pyproject.toml` — add `pydantic-ai-slim`.

---

## Conventions

- TDD red-green-refactor, one failing test at a time, commit per cycle. Behaviour-driven tests only — no shape-only `isinstance` tests.
- Test helpers use explicit kwargs defaults (no `kw.pop` chains, no `dict()` literals) — see `feedback_test_helpers`.
- The agent unit suite uses PydanticAI `TestModel` (no tool calls scripted) and `FunctionModel` (when scripting tool sequences). No real API calls.
- IDs in fixtures: `uuid.uuid4().hex[:8]` with prefix.
- Commit subjects follow `<type>(<scope>): <subject>`.

---

## Task 0: Add pydantic-ai-slim

- [ ] `uv add 'pydantic-ai-slim[openai]'` (the openai extra brings DeepSeek-compatible plumbing).
- [ ] Verify `uv run python -c "from pydantic_ai import Agent; from pydantic_ai.models.test import TestModel; print('ok')"`.
- [ ] Commit `chore(deps): add pydantic-ai-slim for agent runtime`.

---

## Task 1: AgentDeps + RunStep models

**Files:** `src/email_agent/models/agent.py`.

These types are referenced by every later test, but they're typed pass-throughs — no behaviour to drive. Build them as the **first behaviour test demands them** rather than as their own task: when `test_assistant_agent` first imports `AgentDeps`, the resulting `ImportError` becomes the red. (Avoiding the slice-3 mistake where I planned a "make the type" task.)

So this task is just: write the types when task 2's first test demands them. No standalone commit.

---

## Task 2: AssistantAgent.run with TestModel — smoke

**Files:** `src/email_agent/agent/assistant_agent.py`, `src/email_agent/models/agent.py` (created on demand), `tests/unit/agent/test_assistant_agent.py`.

- [ ] **Step 1 (red):** Test constructs an `AssistantAgent`, overrides the model with `TestModel(custom_output_text="hello back")`, and calls `await assistant_agent.run(scope, prompt="hi", deps=...)`. Asserts `result.body == "hello back"`. Imports `AgentDeps` from `email_agent.models.agent` (which doesn't exist yet — that's the red).
- [ ] **Step 2 (green):** Create `AgentDeps`, then `AssistantAgent` with a method `def _agent_for(scope) -> Agent[AgentDeps, str]` that builds (and caches per `(model_name, system_prompt)`) a `pydantic_ai.Agent`. Implement `.run(scope, *, prompt, deps) -> AgentResult` that delegates to `agent.run(prompt, deps=deps)`.
- [ ] Commit `feat(agent): AssistantAgent runs prompts via PydanticAI`.

---

## Task 3: AssistantAgent — read/write/edit/bash route to sandbox

- [ ] **Step 1 (red):** Test uses `FunctionModel` to script a model that calls `read("emails/t-1/thread.md")` then returns "OK". The `FunctionModel` callback receives the tool result, asserts it equals what the in-memory sandbox holds, and returns final text. Outer assertion: `await agent.run(...)` returns "OK"; `InMemorySandbox.run_tool` was invoked with `ToolCall(kind="read", path=...)`.
- [ ] **Step 2 (green):** Define the four tool functions inside `AssistantAgent._build_agent` decorated with `@agent.tool`. Each calls `await ctx.deps.sandbox.run_tool(ctx.deps.assistant_id, ctx.deps.run_id, ToolCall(kind=..., ...))` and returns either the string output (read), the `BashResult` (bash), or `None` (write/edit).
- [ ] **Step 3 (red+green):** Add similar tests for `write`, `edit`, `bash`. Each is a small commit.
- [ ] Commit `feat(agent): file + bash tools route through sandbox`.

---

## Task 4: AssistantAgent — memory_search bypasses sandbox

- [ ] **Step 1 (red):** Test stages an `InMemoryMemoryAdapter` with two memories for `assistant_id=a-1`, scripts a model that calls `memory_search("project alpha")`, asserts the returned list equals the staged memories. Importantly, the in-memory sandbox's `run_tool` is NOT called.
- [ ] **Step 2 (green):** Add the `memory_search` tool that calls `ctx.deps.memory.search(ctx.deps.assistant_id, query)` and returns `list[Memory]`.
- [ ] Commit `feat(agent): memory_search tool bypasses sandbox`.

---

## Task 5: AssistantAgent — attach_file appends to pending_attachments

- [ ] **Step 1 (red):** Test scripts a model that calls `attach_file(path="report.pdf", filename="renamed.pdf")` then returns "done". Outer assertion: `deps.pending_attachments == [PendingAttachment(sandbox_path="report.pdf", filename="renamed.pdf")]`.
- [ ] **Step 2 (green):** Add the `attach_file` tool. Validates the path through the sandbox (returns ToolResult so model knows it succeeded), then appends to `ctx.deps.pending_attachments`.
- [ ] Commit `feat(agent): attach_file records pending attachments`.

---

## Task 6: ReplyEnvelopeBuilder — pure builder

**Files:** `src/email_agent/domain/reply_envelope.py`, `tests/unit/domain/test_reply_envelope.py`.

- [ ] **Step 1 (red):** Test calls `ReplyEnvelopeBuilder().build(inbound, thread, body, attachments=[], message_id_factory=...)` and asserts the envelope's threading headers chain correctly off the inbound, the subject is `Re:`-prefixed (without doubling), and attachments pass through.
- [ ] **Step 2 (green):** Implement the builder. Same `Re:` + `References = inbound.references + [inbound.message_id]` rules slice-3's budget-reply uses.
- [ ] **Step 3:** Refactor `domain/budget_reply.py` to call this builder for the envelope construction; the budget body string stays where it is. Run slice-3 tests, confirm green.
- [ ] Commit `feat(domain): ReplyEnvelopeBuilder shared between agent + budget replies`.

---

## Task 7: accept_inbound also writes agent_runs(status="queued")

**Files:** `src/email_agent/runtime/assistant_runtime.py`, `tests/unit/runtime/test_runtime.py`.

- [ ] **Step 1 (red):** Existing test for `accept_inbound` passes. Add a new test that, after accept_inbound, asserts an `AgentRun` row exists for `(assistant_id, inbound_message_id)` with `status="queued"`. Idempotency test: a second call with the same payload must not create a second `AgentRun` row.
- [ ] **Step 2 (green):** In `accept_inbound`, after persisting the inbound message, write or upsert `AgentRun(id=..., assistant_id, thread_id, inbound_message_id, status="queued")`. Idempotent on `inbound_message_id` (need a unique constraint or a query-then-insert pattern; prefer the unique constraint via Alembic migration).
- [ ] Commit `feat(runtime): accept_inbound queues an agent_runs row`. (Includes the migration if a unique constraint is added.)

---

## Task 8: RunRecorder.record_completion

**Files:** `src/email_agent/domain/run_recorder.py`, `tests/unit/domain/test_run_recorder.py`.

- [ ] **Step 1 (red):** Test seeds a queued `AgentRun` + inbound message + a `SentEmail` receipt. Calls `RunRecorder(session_factory).record_completion(CompletedRun(run_id=..., outbound=..., sent=..., steps=[...], usage=RunUsage(...)))`. Asserts: an `email_messages(direction='outbound')` row exists with the right `message_id_header`; `agent_runs.status == "completed"` with `completed_at` set and `reply_message_id` pointing at the new outbound row; matching `run_steps` rows; matching `usage_ledger` row; an entry in `MessageIndex` for the outbound `Message-ID`.
- [ ] **Step 2 (green):** Implement the writes inside one transaction.
- [ ] **Step 3 (red):** Test idempotency — a second call with the same `(assistant_id, provider_message_id)` for the outbound is a no-op (relies on the existing unique constraint on `email_messages`).
- [ ] **Step 4 (green):** Wrap the outbound insert in an "exists?" check or catch the `IntegrityError` and rollback only the outbound write while leaving the rest intact (probably easier: do the existence check first).
- [ ] **Step 5 (red+green):** Add `record_failure(run_id, error)` that sets `agent_runs.status="failed"`, `error=str(exc)`, `completed_at=now`. No outbound row written.
- [ ] Commit `feat(domain): RunRecorder writes outbound + steps + usage transactionally`.

---

## Task 9: AssistantRuntime.execute_run — happy path with InMemory adapters

**Files:** `src/email_agent/runtime/assistant_runtime.py`, `tests/unit/runtime/test_execute_run.py`.

- [ ] **Step 1 (red):** Test wires `InMemoryEmailProvider`, `InMemoryMemoryAdapter`, `InMemorySandbox`, a `FunctionModel` scripting `read("emails/t-1/0001-…md") → "Re: thanks!"`. Calls `runtime.accept_inbound(email)` then `runtime.execute_run(run_id)`. Asserts:
  - `InMemoryEmailProvider.sent` has one envelope with body `"Re: thanks!"` and threading headers chained off the inbound.
  - `agent_runs.status == "completed"`, `reply_message_id` set.
  - `usage_ledger` has one row with non-zero token counts (TestModel returns deterministic counts).
  - `run_steps` has at least one row.
- [ ] **Step 2 (green):** Compose the orchestrator inside `AssistantRuntime.execute_run`:
  ```python
  async def execute_run(self, run_id: str) -> RunOutcome:
      run, scope, inbound, thread = await self._load_run(run_id)
      decision = await self._budget.decide(scope)
      if isinstance(decision, BudgetLimitReply):
          envelope = build_budget_limit_reply(...)
          sent = await self._email_provider.send_reply(envelope)
          await self._recorder.record_budget_limited(run_id, sent, decision)
          return BudgetLimited(...)

      projection = self._projector.project(...)
      await self._sandbox.ensure_started(scope.assistant_id)
      await self._sandbox.project_emails(scope.assistant_id, _read_files(projection.run_inputs_dir))
      memory = await self._memory.recall(scope.assistant_id, thread.id, query=...)

      deps = AgentDeps(...)
      try:
          result = await self._agent.run(scope, prompt=..., deps=deps)
      except Exception as exc:
          await self._recorder.record_failure(run_id, str(exc))
          raise

      attachment_bytes = [
          (a, await self._sandbox.read_attachment_out(scope.assistant_id, run_id, a.sandbox_path))
          for a in deps.pending_attachments
      ]
      envelope = self._envelope_builder.build(inbound, thread, result.body, attachment_bytes)
      sent = await self._email_provider.send_reply(envelope)
      await self._recorder.record_completion(CompletedRun(...))
      return Completed(run_id=run_id, sent=sent)
  ```
- [ ] Commit `feat(runtime): execute_run orchestrates the full agent pipeline`.

---

## Task 10: execute_run — budget-limited path

- [ ] **Step 1 (red):** Test stages a `usage_ledger` row that takes the assistant at-cap before the run. `execute_run` should send a budget-limit template via the email provider, record the run as `"budget_limited"`, and never invoke the agent or sandbox.
- [ ] **Step 2 (green):** The budget-limited branch already exists in the orchestrator; this test just locks it in. Add a `record_budget_limited` method to `RunRecorder` if needed.
- [ ] Commit `test(runtime): execute_run sends template + records when budget exceeded`.

---

## Task 11: execute_run — failure path

- [ ] **Step 1 (red):** Test scripts a `FunctionModel` that raises mid-run. Asserts: `execute_run` records the run as `"failed"` with the exception message, sends NO reply, and re-raises (or swallows — pick one based on the design's "default off" fallback policy).
- [ ] **Step 2 (green):** Wrap the agent + post-agent steps in a try/except; on exception, call `RunRecorder.record_failure` and re-raise. Procrastinate's retry behaviour will pick up from there in slice 7.
- [ ] Commit `feat(runtime): execute_run records failed runs and re-raises`.

---

## Task 12: execute_run — per-run wall-clock timeout

- [ ] **Step 1 (red):** Test passes `runtime = AssistantRuntime(..., run_timeout_seconds=1)`, scripts a `FunctionModel` that sleeps 5s. Asserts `execute_run` aborts within ~1.5s, records the run as `"failed"` with a timeout error.
- [ ] **Step 2 (green):** Wrap the agent run in `asyncio.wait_for(...)`; on `TimeoutError`, record_failure with a typed reason.
- [ ] Commit `feat(runtime): execute_run enforces per-run wall-clock timeout`.

---

## Task 13: DeepSeek smoke (gated)

**Files:** `tests/integration/test_deepseek_smoke.py`.

- [ ] Create one test marked `integration` that skips unless `os.environ.get("EMAIL_AGENT_E2E") == "1"`. Builds an `AssistantAgent` configured with the OpenAI-compatible provider pointing at `https://api.deepseek.com/v1`, runs a 1-token-ish prompt, asserts the result is non-empty.
- [ ] Commit `test: gated DeepSeek smoke for the OpenAI-compatible provider wiring`.

---

## Task 14: Re-run full suite + lint + types

- [ ] `uv run pytest -q` (unit only)
- [ ] `uv run pytest -m integration` (with docker, no E2E flag)
- [ ] `EMAIL_AGENT_E2E=1 uv run pytest tests/integration/test_deepseek_smoke.py` (verify locally once)
- [ ] `uv run ruff check && uv run ruff format --check && uv run ty check`

---

## Done when

- An inbound email queued by `accept_inbound` produces a recorded `agent_runs` row with `status="completed"` (or `"budget_limited"` / `"failed"`) when `execute_run` is invoked.
- `AssistantAgent` exposes the six tools; `read/write/edit/bash` route through `AssistantSandbox`; `memory_search` bypasses the sandbox; `attach_file` accumulates in `pending_attachments`.
- `ReplyEnvelopeBuilder` is the single source of subject + threading-header rules; the budget-limit reply uses it.
- `RunRecorder` is idempotent on duplicate `(assistant_id, provider_message_id)`.
- `AssistantRuntime.execute_run` enforces budget gating, per-run wall-clock timeout, and records failures.
- All slice-5 unit tests + lint + types green; the docker integration suite still green; the gated DeepSeek smoke runs cleanly when `EMAIL_AGENT_E2E=1`.

Cognee adapter (slice 6), Procrastinate worker (slice 7), and admin UI (slice 8) remain ahead.
