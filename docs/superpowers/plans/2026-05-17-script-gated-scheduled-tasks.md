# Script-gated scheduled tasks

**Goal:** Make scheduled tasks cheaper, quieter, and more reliable by letting
them run deterministic sandbox commands before deciding whether to involve the
LLM or notify the user.

Today every scheduled task becomes a synthetic inbound email, which always
queues an agent run. That is wasteful for ambient checks where most ticks have
nothing useful to report. After this slice, a scheduled task can optionally run
a bash command inside the assistant sandbox first:

- command exits `0` → continue with stdout as the useful payload.
- command exits `1` → quiet no-op; mark the task fired, send no email, call no LLM.
- command exits `2+` → command failure; record failure and leave the task active
  for retry/failure policy.

The scheduled task's `name` remains the email subject. The command only decides
whether there is something worth saying, and what content should be passed on.

## Product requirements

- Preserve current scheduled-task behaviour when no command is configured.
- Allow scheduled tasks to run a deterministic bash command in the assistant's
  existing sandbox/workspace before agent execution.
- Add `is_agent_enabled` to choose the post-command path:
  - `true`: stdout becomes the synthetic inbound body for the agent.
  - `false`: stdout is sent directly to the user as the email body.
- Support quiet exits before the LLM: command exit `1` means no user-visible
  notification and no agent run.
- Support quiet exits after the LLM: if the agent replies exactly
  `QUIETLY_EXIT`, no email is sent and the run is recorded as intentionally
  quiet.
- Keep the durable task format independent of Pydantic AI CodeMode. CodeMode may
  remain enabled for normal agent runs, but scheduled commands should not depend
  on it.
- Run commands through the same sandbox/container abstraction as agent bash
  tool calls, with the same timeout/resource controls.
- Keep scheduled task subjects simple: use `scheduled_tasks.name` for both
  agent-generated and direct command emails.
- Record enough audit/debug data to explain why a scheduled task did or did not
  notify the user.
- Add an unanswered-nudge protection: pause recurring scheduled tasks after a
  configurable number of user-unanswered notification runs, defaulting to `3`.

## Non-goals

- Do not add a host-side `email-agent-tool` CLI in this slice.
- Do not make CodeMode/Monty code the primary scheduled-task command format.
- Do not require command stdout to be JSON for the MVP.
- Do not add arbitrary host credentials or direct DB access to the sandbox.
- Do not solve full MCP access from scheduled commands yet. If needed later,
  add a host-mediated tool bridge as a separate design.

## Data model

Extend `scheduled_tasks`:

| Column | Type | Meaning |
| --- | --- | --- |
| `command` | `Text`, nullable | Bash command to run in the assistant sandbox before dispatch. `NULL` keeps current direct-agent behaviour. |
| `is_agent_enabled` | `Boolean`, not null, default `true` | If command exits `0`, choose whether stdout goes to the agent or directly to email. |
| `max_unanswered_runs` | `Integer`, nullable/default `3` | Number of notification runs allowed without a user reply before pausing. |
| `consecutive_unanswered_runs` | `Integer`, not null, default `0` | Counter used by the pause policy. |
| `paused_reason` | `Text`, nullable | Human-readable reason when the task is paused automatically. |

Add a lightweight audit table, or equivalent run-step/admin-visible record:

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | string pk | Fire/check id. |
| `scheduled_task_id` | fk | Task that ticked. |
| `fired_at` | timestamptz | Tick time. |
| `status` | string | `continued`, `quiet_exited`, `command_failed`, `agent_quiet_exited`, `sent_direct`, `paused`. |
| `exit_code` | int nullable | Command exit code when command ran. |
| `stdout` | text nullable | Truncated command stdout for debugging/audit. |
| `stderr` | text nullable | Truncated command stderr/reason for debugging/audit. |
| `agent_run_id` | fk nullable | Agent run created after the command, if any. |

If a separate audit table feels too much for the first pass, record the same
information in structured logs and defer the table. The table is preferable for
admin UI visibility.

## Command contract

Scheduled command scripts are bash-native:

```text
exit 0:
  Continue.
  stdout is the content.

exit 1:
  Expected quiet no-op.
  stderr explains why nothing was sent.

exit 2+:
  Unexpected failure.
  stderr/stdout are diagnostic output.
```

When `is_agent_enabled=true`, stdout is used as the synthetic inbound body. When
`is_agent_enabled=false`, stdout is sent directly as the email body. In both
cases, the subject is `scheduled_tasks.name`.

Example command:

```bash
python automations/weekend_weather.py
```

Example quiet no-op script branch:

```bash
echo "No nudge: weekend forecast is rainy or no nice-weather ideas were found." >&2
exit 1
```

Example continue script branch:

```bash
cat <<'EOF'
Saturday looks sunny with a high of 24C.
Nice-weather ideas found:
- walk around Merri Creek
- ride to the market
- read in the park

Pick one and send Larry a short nudge.
EOF
exit 0
```

## Example workflows

### Existing direct-agent scheduled task

Configuration:

```text
name: Morning check-in
cron_expr: 0 21 * * *
command: NULL
is_agent_enabled: true
body: Send Larry a short morning check-in.
```

Flow:

1. Tick finds the task due.
2. No command is configured.
3. Existing synthetic inbound path runs unchanged.
4. Agent decides what to send.
5. If agent returns `QUIETLY_EXIT`, no email is sent; otherwise the reply is
   sent normally.

### Nice-weather weekend nudge with agent polish

Configuration:

```text
name: Nice weather idea for this weekend
cron_expr: 0 3 * * 4
command: python automations/weekend_weather.py
is_agent_enabled: true
```

Flow when weather is bad:

1. Tick runs the command in the sandbox.
2. Command exits `1` and writes the no-op reason to stderr.
3. Runtime records a quiet scheduled fire.
4. Task is marked fired/rescheduled.
5. No LLM call and no email.

Flow when weather is good and ideas exist:

1. Tick runs the command in the sandbox.
2. Command exits `0` and writes useful context to stdout.
3. Runtime builds a synthetic inbound using task name as subject and stdout as
   body.
4. Agent runs and turns the context into a concise nudge.
5. Agent may still reply `QUIETLY_EXIT` if it decides the nudge is not worth
   sending.

### Nice-weather weekend nudge with direct email

Configuration:

```text
name: Nice weather idea for this weekend
cron_expr: 0 3 * * 4
command: python automations/weekend_weather.py --email-body
is_agent_enabled: false
```

Flow when weather is good and ideas exist:

1. Tick runs the command.
2. Command exits `0`.
3. Runtime sends stdout directly as the email body.
4. No LLM call.
5. Task is marked fired/rescheduled.

### Automatic pause after unanswered nudges

Configuration:

```text
name: Weekly planning nudge
cron_expr: 0 23 * * 0
max_unanswered_runs: 3
```

Flow:

1. Each user-visible scheduled notification increments
   `consecutive_unanswered_runs` if the user has not replied since the previous
   scheduled notification.
2. Any real inbound reply from the user resets the counter.
3. When the counter reaches `max_unanswered_runs`, the task is paused.
4. Runtime sends one final notification explaining that the recurring nudge was
   paused due to no replies.

## Runtime architecture

Refactor the current `AssistantRuntime.execute_run()` setup so scheduled
preflight and normal agent execution share tool/dependency construction.

Introduce a prepared run context:

```python
@dataclass
class PreparedAssistantRun:
    scope: AssistantScope
    workspace: AssistantWorkspace
    deps: AgentDeps
    prompt: str
    pending_attachments: list[PendingAttachment]
    metered_usage: list[MeteredUsage]
```

Initial extraction:

```python
async def prepare_assistant_run(
    self,
    *,
    run_id: str,
    scope: AssistantScope,
    thread: EmailThread,
    inbound_email: NormalizedInboundEmail,
    record_steps: bool = True,
) -> PreparedAssistantRun:
    ...
```

Normal execution uses the prepared context:

```python
prepared = await self.prepare_assistant_run(...)
agent_result = await self._agent.run(prepared.scope, prompt=prepared.prompt, deps=prepared.deps)
```

Scheduled command execution can reuse the same workspace/sandbox/tool context
without calling the agent:

```python
prepared = await self.prepare_assistant_run(...)
result = await prepared.workspace.environment.exec(task.command)
```

If this makes prompt preparation too expensive for command-only tasks, split the
helper later into:

- `prepare_tool_context(...)`
- `prepare_agent_prompt(...)`

Start with the smallest extraction that removes duplicated `AgentToolset` and
`AgentDeps` construction.

## Handoff notes

These are the details most likely to save implementation time.

### Current relevant files

- `src/email_agent/scheduled/tick.py` owns due-task claiming and synthetic
  inbound dispatch.
- `src/email_agent/scheduled/service.py` owns scheduled-task CRUD and
  rescheduling.
- `src/email_agent/models/scheduled.py` is the pydantic domain model.
- `src/email_agent/db/models.py` contains `ScheduledTaskRow` and
  `AgentRun.triggered_by_scheduled_task_id`.
- `src/email_agent/agent/toolset.py` exposes `create_scheduled_task`,
  `list_scheduled_tasks`, and `delete_scheduled_task` to the agent.
- `src/email_agent/runtime/assistant_runtime.py` currently builds
  `AgentToolset`/`AgentDeps` inline inside `execute_run()`.
- `src/email_agent/jobs/app.py` registers the once-per-minute
  `tick_scheduled_tasks` Procrastinate periodic task.
- `src/email_agent/sandbox/environment.py` defines the sandbox `exec` surface
  and `ShellResult` shape.
- `tests/unit/scheduled/test_tick.py` already has the best starting point for
  tick behaviour tests.
- `tests/unit/runtime/test_execute_run.py` is the right home for agent
  `QUIETLY_EXIT` tests.

### Suggested implementation order

Start with the smallest behavioural change and defer broad refactors until a
test forces them:

1. Add the quiet no-op command test in `test_tick.py`.
2. Add only enough schema/model plumbing for a task to have `command` and
   `is_agent_enabled`.
3. Introduce a tiny fake command runner dependency for tick tests rather than
   wiring the real sandbox immediately.
4. Make exit `1` quiet-no-op pass.
5. Add exit `0` agent-enabled test and direct-email test.
6. Add real sandbox command execution once the branching semantics are pinned.
7. Refactor shared `AgentDeps`/`AgentToolset` preparation only when command
   execution needs the real assistant workspace.
8. Add agent `QUIETLY_EXIT`.
9. Add unanswered pause policy last; it depends on the previous definition of
   "user-visible notification".

### Dependency injection shape

`tick_scheduled_tasks_impl()` is currently pure-ish and testable because it
takes `runtime`, `service`, `session_factory`, and `now`. Keep that property.
Do not make unit tests spin up Docker or a worker.

One pragmatic shape:

```python
class ScheduledCommandRunner(Protocol):
    async def run(self, task: ScheduledTaskRow) -> ShellResult: ...
```

Then:

```python
async def tick_scheduled_tasks_impl(
    *,
    runtime: _RuntimeLike,
    service: ScheduledTaskService,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime,
    command_runner: ScheduledCommandRunner | None = None,
    direct_sender: ScheduledDirectSender | None = None,
) -> None:
    ...
```

Production can pass a runner built from the runtime/workspace provider. Unit
tests can pass a fake that returns fixed `ShellResult`s.

If passing extra collaborators into `tick_scheduled_tasks_impl()` feels noisy,
wrap them in a small `ScheduledTaskDispatcher`. Keep tests at the behaviour
boundary either way.

### Direct-email dependency

The tick path does not currently send email directly; it only calls
`runtime.accept_inbound()`. Direct-email mode needs a narrow abstraction rather
than reaching into Mailgun from `tick.py`.

Suggested protocol:

```python
class ScheduledDirectSender(Protocol):
    async def send(
        self,
        *,
        assistant_id: str,
        to_email: str,
        subject: str,
        body_text: str,
    ) -> None: ...
```

Production implementation can live near runtime/composition and reuse the
existing email provider/rendering/footer conventions. Tests can assert calls on
a fake sender.

### Command execution details

- Execute `task.command` through the assistant sandbox, not host
  `subprocess`.
- Apply the same bash timeout/resource limits used by the `bash` tool.
- Trim or reject empty stdout on exit `0`. For MVP, treat empty stdout as
  command failure rather than sending a blank email or blank agent prompt.
- Truncate stdout/stderr before persisting audit/debug rows.
- Exit `1` should advance once tasks to completed and reschedule cron tasks.
- Exit `2+` should not advance `last_run_at` unless the audit design explicitly
  records failed attempts separately from successful fires.

### Prompt/body details

When command exit `0` and `is_agent_enabled=true`, use the command stdout as the
synthetic inbound body. It is fine to keep the existing trigger marker prefix:

```text
[Triggered by scheduled task '...' (...) at ...]

<command stdout>
```

Do not append the original `scheduled_tasks.body` unless a later test says that
is useful. For command-backed tasks, the command output is the payload.

When `command is NULL`, keep using `scheduled_tasks.body` exactly as today.

### Agent tool guidance

The existing scheduling starter skill in `src/email_agent/sandbox/skills.py`
only documents `kind`, `when`, `name`, and `body`. Once command-backed tasks are
exposed to the agent, update that skill so the agent knows:

- use a command for ambient checks that often have nothing to report;
- exit `0` with stdout to continue;
- exit `1` with stderr for expected quiet no-op;
- exit `2+` for real failure;
- use `QUIETLY_EXIT` exactly when an agent-run scheduled task has nothing useful
  to send.

If `create_scheduled_task` grows more arguments, keep backward compatibility for
existing calls.

### Status naming

Prefer adding a new run status such as `quiet_exited` over overloading
`completed` for agent quiet exits. Admin UI and tests can then distinguish:

- completed and email sent;
- quiet-exited intentionally;
- failed.

If changing statuses has too much blast radius, record `completed` with a clear
step/audit marker as a fallback, but that is less useful operationally.

### Edge cases to test or consciously defer

- Command exits `0` with whitespace-only stdout.
- Command exits `1` with empty stderr.
- Cron task command exit `1` should compute the next cron occurrence from the
  tick time, same as successful fires.
- Once task command exit `1` should become completed.
- Command exit `2+` should not send pause notifications or increment
  unanswered-run counters.
- Direct email send failure should leave the task active or record failure
  consistently with `accept_inbound` failure handling.
- Multiple workers ticking concurrently should still avoid double-firing via
  the existing row lock path.

## Tick behaviour

Current due-row claiming stays the same: active rows with `next_run_at <= now`,
with Postgres `SELECT FOR UPDATE SKIP LOCKED`.

Pseudo-flow:

```python
for task in due_tasks:
    if task.command is None:
        await dispatch_existing_synthetic_inbound(task)
        continue

    result = await run_scheduled_command(task)

    if result.returncode == 1:
        await record_fire(task, status="quiet_exited", stderr=result.stderr)
        await mark_fired(task)
        continue

    if result.returncode != 0:
        await record_fire(task, status="command_failed", stdout=result.stdout, stderr=result.stderr)
        continue

    if task.is_agent_enabled:
        outcome = await dispatch_synthetic_inbound(task, body=result.stdout)
        await record_fire(task, status="continued", stdout=result.stdout, agent_run_id=...)
        await mark_fired_if_accepted(task, outcome)
    else:
        await send_direct_scheduled_email(task, body=result.stdout)
        await record_fire(task, status="sent_direct", stdout=result.stdout)
        await mark_fired(task)
```

Important semantics:

- Exit `1` is success/no-op, so the task should advance/reschedule.
- Exit `2+` is failure, so the task should remain due unless/until retry policy
  says otherwise.
- Router drops or `accept_inbound` exceptions should keep the task active, as
  they do today.

## Agent quiet exit

Add a strict sentinel for agent output:

```text
QUIETLY_EXIT
```

If `agent_result.body.strip() == "QUIETLY_EXIT"`:

- do not send an email;
- record the run as `quiet_exited` or equivalent;
- persist usage, steps, metered usage, and completion time;
- mark/reschedule the scheduled task if this was scheduled-origin;
- do not treat this as failure.

Use exact whole-output matching only. Do not suppress emails that merely mention
the phrase.

Update scheduling skill/system guidance:

```text
If a scheduled task fires but there is nothing useful to tell the user, respond
exactly: QUIETLY_EXIT
```

## TDD approach

Use red-green-refactor throughout. Tests must encode behaviours that matter to
users or operators, not merely assert that new fields or methods exist.

Avoid tests whose only failure is "attribute is missing" or "function is not
implemented". A good red test should describe an observable outcome:

- whether an agent run was queued;
- whether an email was sent;
- whether a task advanced/rescheduled or stayed active;
- whether quiet/failure reasons were recorded;
- whether a run was completed, quiet-exited, or failed.

Prefer extending the existing scheduled/runtime tests over adding isolated
shape-only tests. Schema/model round-trip tests are still useful, but they
should support behaviour tests rather than lead the design.

### Behaviour-first red tests

Add these tests before implementation, roughly in this order:

1. Existing scheduled tasks still work when `command=None`.
   - Given a due once task with no command.
   - When the scheduled tick runs.
   - Then a synthetic inbound is accepted.
   - And the task is completed/rescheduled as it is today.

2. Command exit `1` is a quiet no-op.
   - Given a due task with a command.
   - And the sandbox command returns `returncode=1` with a no-op reason on
     stderr.
   - When the scheduled tick runs.
   - Then no synthetic inbound is accepted.
   - And no email is sent.
   - And the task is marked fired/rescheduled.
   - And the quiet reason is recorded somewhere observable.

3. Command exit `0` with `is_agent_enabled=True` feeds stdout to the agent path.
   - Given a command returns `stdout="Saturday is sunny..."`.
   - When the scheduled tick runs.
   - Then `runtime.accept_inbound()` receives a synthetic email.
   - And the synthetic email body includes command stdout.
   - And the task is marked fired/rescheduled.

4. Command exit `0` with `is_agent_enabled=False` sends stdout directly.
   - Given a command returns `stdout="Saturday is sunny. Go for a walk."`.
   - When the scheduled tick runs.
   - Then no synthetic inbound is accepted.
   - And the email provider sends one email.
   - And the subject is `scheduled_tasks.name`.
   - And the body is command stdout.

5. Command exit `2+` is a failure, not a quiet no-op.
   - Given a command returns `returncode=2` and `stderr="API failed"`.
   - When the scheduled tick runs.
   - Then no synthetic inbound is accepted.
   - And no email is sent.
   - And the task remains active/due for retry.
   - And failure details are recorded.

6. Agent `QUIETLY_EXIT` suppresses email.
   - Given a queued run where the model returns exactly `QUIETLY_EXIT`.
   - When `execute_run()` runs.
   - Then no reply email is sent.
   - And the run is recorded as quiet/completed, not failed.
   - And usage/steps are still persisted.

7. `QUIETLY_EXIT` is exact only.
   - Given the model returns text that merely mentions `QUIETLY_EXIT`.
   - When `execute_run()` runs.
   - Then the email is sent normally.

8. Unanswered-run pausing.
   - Given a recurring scheduled task with `max_unanswered_runs=3`.
   - And three user-visible scheduled notifications happen without a user reply.
   - Then the task is paused.
   - And one pause notification is sent.
   - And future ticks do not fire it.

Suggested first red test:

```python
async def test_tick_command_exit_1_quietly_marks_fired_without_agent_run(...):
    ...
```

This is the smallest test that captures the central product value: an ambient
scheduled check can decide there is nothing useful to say without spending an
LLM call or notifying the user.

## Implementation tasks

### Task 1 — Schema and models

- Add `command`, `is_agent_enabled`, unanswered-pause fields to scheduled task
  ORM/domain models.
- Add Alembic migration.
- Add round-trip tests for new fields.

### Task 2 — Runtime preparation refactor

- Extract `prepare_assistant_run()` from `AssistantRuntime.execute_run()`.
- Keep existing behaviour green.
- Add tests proving normal `execute_run()` still constructs the same toolset
  paths and records prompt metadata.

### Task 3 — Scheduled command runner

- Add command execution through the assistant sandbox.
- Implement exit-code contract.
- Add unit tests for exit `0`, `1`, and `2+`.

### Task 4 — Tick integration

- Wire command execution into `tick_scheduled_tasks_impl`.
- Preserve current no-command behaviour.
- Test:
  - `command=None` still dispatches synthetic inbound directly.
  - exit `1` marks/reschedules without agent run.
  - exit `0` + `is_agent_enabled=true` dispatches synthetic inbound with stdout
    body.
  - exit `0` + `is_agent_enabled=false` sends direct email and creates no agent
    run.
  - exit `2+` records failure and leaves the task active.

### Task 5 — Agent quiet exit

- Add `QuietExited` runtime outcome.
- Record quiet completions without sending email.
- Update admin status styling if needed.
- Update system/scheduling skill guidance.

### Task 6 — Unanswered-run pause policy

- Define how to detect "user replied" for scheduled-task-linked notifications.
- Increment/reset `consecutive_unanswered_runs`.
- Pause recurring tasks at `max_unanswered_runs`.
- Send one final pause notification.

### Task 7 — Admin visibility

- Show command, `is_agent_enabled`, status, last fire, and paused reason in the
  admin UI.
- Show scheduled fire audit rows if the audit table lands in this slice.

### Task 8 — End-to-end tests

- Add integration coverage for a script-gated recurring task.
- Cover quiet no-op and continue-to-agent paths.
- Cover direct email path with `is_agent_enabled=false`.

## Open questions

- Should direct-email mode run through the same footer/rendering pipeline as
  agent replies? Default should be yes, unless that causes confusing wording.
- Should exit `2+` retry forever, or pause after N command failures? Start by
  leaving it active and logging/auditing; add a failure threshold if noisy.
- Should command stdout/stderr be truncated before persistence? Yes; choose a
  limit aligned with run-step summaries or admin display constraints.
- Should command execution happen before or after memory recall/prompt
  preparation? Prefer before expensive prompt prep once the helper is split.
- Should once tasks that quiet-exit be marked completed? Yes: exit `1` means an
  expected no-op for that scheduled fire.
