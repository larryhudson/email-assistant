# Slice 3 — Budget + Template Replies Implementation Plan

**Goal:** Decide whether an inbound run can proceed under the assistant's monthly spend cap, and when it can't, send a cheap canned reply via the real Mailgun API. Build the components; final wiring into the run loop happens in slice 5.

**Architecture:** Three new pieces.

1. `domain/budget_governor.py` — `BudgetGovernor` reads the assistant's `usage_ledger` rows for the active budget period and returns `Allow` or `BudgetLimitReply` based on `Budget.monthly_limit_cents`.
2. `domain/budget_reply.py` — pure function `build_budget_limit_reply(inbound, scope, decision) -> NormalizedOutboundEmail`. Uses the inbound's threading headers + a static template body. No model call.
3. `mail/mailgun.py::MailgunEmailProvider.send_reply` — real implementation. Posts multipart/form-data to `https://api.mailgun.net/v3/{domain}/messages` with the threading headers (`h:Message-Id`, `h:In-Reply-To`, `h:References`). Returns a `SentEmail`.

The webhook fast path stays unchanged in this slice — `BudgetGovernor` will be called from `execute_run` when slice 5 lands. `accept_inbound` doesn't gate on budget; that's a worker concern.

**Tech stack additions:** `httpx` graduates from dev-only to runtime dep (Mailgun HTTP client).

**Out of scope (slice 5+):** wiring `BudgetGovernor` into a worker, `agent_runs` row writes, `RunRecorder`, Procrastinate `run_agent` job, threshold notifications.

---

## File Structure

**Create:**
- `src/email_agent/domain/budget_governor.py` — `BudgetGovernor`, `Allow`, `BudgetLimitReply`, `BudgetDecision`.
- `src/email_agent/domain/budget_reply.py` — `build_budget_limit_reply`.
- `tests/unit/test_budget_governor.py` — sqlite tests for under/over/at-limit and period boundaries.
- `tests/unit/test_budget_reply.py` — pure-function tests for the template envelope.
- `tests/unit/test_mailgun_send_reply.py` — `httpx.MockTransport` tests for the real `send_reply` (auth, threading headers, attachments).

**Modify:**
- `src/email_agent/mail/mailgun.py` — implement `send_reply`, accept `api_key` + `domain` in `__init__`.
- `src/email_agent/mail/__init__.py` — no surface change; just keep `MailgunEmailProvider` exported.
- `pyproject.toml` — move `httpx` to runtime `[project.dependencies]`.

---

## Conventions

- TDD red-green-refactor, one failing test at a time, commit per cycle.
- sqlite+aiosqlite via `sqlite_session_factory` fixture for governor tests.
- IDs in fixtures: `uuid.uuid4().hex[:8]` with prefix (`a-`, `b-`, `r-`, …).
- `httpx.MockTransport` for Mailgun HTTP — no real network in unit tests.
- Commit subjects follow `<type>(<scope>): <subject>`.

---

## Task 0: Promote httpx to runtime dep

- [ ] **Step 1:** `uv remove --dev httpx && uv add httpx`.
- [ ] **Step 2:** Verify `uv run python -c "import httpx; print(httpx.__version__)"`.
- [ ] **Step 3:** Commit `chore(deps): promote httpx to runtime dependency`.

---

## Task 1: BudgetGovernor.decide — under-limit returns Allow

**Files:** `src/email_agent/domain/budget_governor.py`, `tests/unit/test_budget_governor.py`.

- [ ] **Step 1 (red):** Test seeds an `Assistant` + `Budget` with `monthly_limit_cents=1000` and a single `UsageLedger` row of 100 cents in the active period. Calls `BudgetGovernor(session_factory).decide(scope)` and asserts `isinstance(decision, Allow)`.
- [ ] **Step 2 (green):** Implement `BudgetGovernor.decide` — `SELECT SUM(cost_cents) FROM usage_ledger WHERE assistant_id = :id AND created_at >= budget.period_starts_at AND created_at < budget.period_resets_at`. Return `Allow` when the sum is strictly less than `monthly_limit_cents`.
- [ ] **Step 3:** Commit `feat(domain): BudgetGovernor allows under-limit assistants`.

---

## Task 2: BudgetGovernor.decide — at/over limit returns BudgetLimitReply

- [ ] **Step 1 (red):** Add a test where ledger sum equals the cap, asserting `BudgetLimitReply(monthly_limit_cents=1000, spent_cents=1000, days_until_reset=...)`. `days_until_reset` calculated from `Budget.period_resets_at - now`.
- [ ] **Step 2 (green):** In `decide`, return `BudgetLimitReply` when `spent >= cap`. Inject a `now` callable (default `datetime.now(UTC)`) to make the reset calculation testable.
- [ ] **Step 3:** Add a second test with `spent > cap` to lock in the `>=` behaviour. Make sure it passes without further changes.
- [ ] **Step 4:** Commit `feat(domain): BudgetGovernor blocks at-or-over-limit assistants`.

---

## Task 3: BudgetGovernor.decide — ignores ledger rows outside the active period

- [ ] **Step 1 (red):** Test seeds two ledger rows: one before `period_starts_at`, one inside. Total exceeds cap, but only the in-period row counts (under cap). Assert `Allow`.
- [ ] **Step 2 (green):** Confirm the WHERE clause already filters by `created_at`. If the sqlite-vs-Postgres timezone behaviour bites, normalize to UTC explicitly.
- [ ] **Step 3:** Commit `test(domain): BudgetGovernor scopes ledger sum to active period`.

---

## Task 4: build_budget_limit_reply — pure builder

**Files:** `src/email_agent/domain/budget_reply.py`, `tests/unit/test_budget_reply.py`.

- [ ] **Step 1 (red):** Test constructs a `NormalizedInboundEmail` (subject `"Question?"`, sender `mum@example.com`, message-id `<m1@x>`, references `["<r0@x>"]`) and a `BudgetLimitReply(monthly_limit_cents=1000, spent_cents=1000, days_until_reset=3)`. Asserts the returned `NormalizedOutboundEmail` has:
  - `from_email == scope.inbound_address`
  - `to_emails == [inbound.from_email]`
  - `subject == "Re: Question?"` (no double `Re:` if already prefixed)
  - body mentions "monthly budget" and "3 days"
  - `in_reply_to_header == "<m1@x>"`
  - `references_headers == ["<r0@x>", "<m1@x>"]`
  - generated `message_id_header` matches `<run-...@<domain>>`
- [ ] **Step 2 (green):** Implement `build_budget_limit_reply(inbound, scope, decision, *, message_id_factory)`. Use the same subject prefix logic as the design's `ReplyEnvelopeBuilder` (slice 5 will share it).
- [ ] **Step 3:** Add a test asserting `subject` is left untouched when it already starts with `re:` (case-insensitive).
- [ ] **Step 4:** Commit `feat(domain): build budget-limit template reply`.

---

## Task 5: MailgunEmailProvider.send_reply — minimal happy path

**Files:** `src/email_agent/mail/mailgun.py`, `tests/unit/test_mailgun_send_reply.py`.

- [ ] **Step 1 (red):** Test uses `httpx.MockTransport` to capture the outgoing request. Constructs `MailgunEmailProvider(signing_key="…", api_key="key-…", domain="mg.example.com")` (new args) and calls `await provider.send_reply(NormalizedOutboundEmail(...))`. Asserts:
  - URL is `https://api.mailgun.net/v3/mg.example.com/messages`
  - method `POST`
  - Basic auth `api:key-…`
  - multipart fields `from`, `to`, `subject`, `text` match the envelope
  - `h:Message-Id`, `h:In-Reply-To`, `h:References` headers populated when present
  - `SentEmail.provider_message_id` taken from the JSON `id` field of the (mocked) response
  - `SentEmail.message_id_header` echoes the envelope's `message_id_header`
- [ ] **Step 2 (green):** Add `api_key` + `domain` to `__init__`. Implement `send_reply` using `httpx.AsyncClient` (build a transport-injectable factory for tests). Strip surrounding `<>` from `Message-Id` for the `h:Message-Id` header per Mailgun's convention (Mailgun re-wraps it).
- [ ] **Step 3:** Commit `feat(mail): MailgunEmailProvider.send_reply via Mailgun HTTP API`.

---

## Task 6: MailgunEmailProvider.send_reply — attachments

- [ ] **Step 1 (red):** Test sends a reply with one `EmailAttachment(filename="x.pdf", content_type="application/pdf", data=b"%PDF...")`. Assert the multipart includes a file part named `attachment` with the right filename, content type, and bytes.
- [ ] **Step 2 (green):** Append each attachment to the multipart `files` list as `("attachment", (filename, data, content_type))`.
- [ ] **Step 3:** Commit `feat(mail): support attachments in Mailgun send_reply`.

---

## Task 7: MailgunEmailProvider.send_reply — error handling

- [ ] **Step 1 (red):** Test returns an HTTP 401 from the mock transport. Assert `send_reply` raises a typed `MailgunSendError` with status code + body excerpt.
- [ ] **Step 2 (green):** Add `MailgunSendError`. `response.raise_for_status()` wrapped to raise the typed error.
- [ ] **Step 3:** Commit `feat(mail): typed error for Mailgun send failures`.

---

## Task 8: Re-run full suite + lint + types

- [ ] `uv run pytest -q`
- [ ] `uv run ruff check`
- [ ] `uv run ruff format --check`
- [ ] `uv run ty check`

If anything fails, fix before final review. No commit unless changes are needed.

---

## Done when

- `BudgetGovernor` returns the right decision for under-, at-, and over-limit, scoped to the active period.
- `build_budget_limit_reply` produces a threading-correct envelope without any LLM call.
- `MailgunEmailProvider.send_reply` posts the right HTTP request, including threading headers and attachments, and surfaces a typed error on failure.
- All slice-3 tests + lint + types green.

Wiring (`BudgetGovernor` invoked before agent execution, sending the template via `MailgunEmailProvider.send_reply`, recording a `BudgetLimited` run) is deferred to slice 5.
