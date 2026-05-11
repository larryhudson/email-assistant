# Architecture Patterns To Consider From Flue

Flue is not a drop-in replacement for this project. It is a TypeScript agent
harness that owns sessions, tools, sandbox environments, context discovery,
schema results, streaming, and deployment targets. This project is a Python
email assistant runtime with routing, budgets, memory, Mailgun, durable runs,
and reply envelopes.

The useful exercise is not "copy Flue features". It is to look for architectural
patterns that make the system deeper, cleaner, and easier to change.

## 1. Split Generic Sandbox Environment From Email Workspace

### Before

`AssistantSandbox` is shaped around the email assistant workflow:

- `ensure_started(assistant_id)`
- `project_emails(assistant_id, files)`
- `project_attachments(assistant_id, run_id, files)`
- `run_tool(assistant_id, run_id, ToolCall)`
- `read_attachment_out(assistant_id, run_id, path)`
- `reset(assistant_id)`

That keeps the public surface small, but it mixes several concerns:

- container lifecycle
- generic filesystem operations
- shell execution
- model-visible tool dispatch
- email thread projection
- attachment staging and extraction
- read-only policy for `/workspace/emails`

### After Sketch

Introduce a lower-level environment interface:

```python
class SandboxEnvironment(Protocol):
    async def exec(self, command: str, *, cwd: str | None = None, timeout_s: int | None = None) -> ShellResult: ...
    async def read_text(self, path: str) -> str: ...
    async def read_bytes(self, path: str) -> bytes: ...
    async def write_text(self, path: str, content: str) -> None: ...
    async def write_bytes(self, path: str, content: bytes) -> None: ...
    async def stat(self, path: str) -> FileStat: ...
    async def readdir(self, path: str) -> list[str]: ...
    async def exists(self, path: str) -> bool: ...
    async def mkdir(self, path: str, *, parents: bool = False) -> None: ...
    async def rm(self, path: str, *, recursive: bool = False, force: bool = False) -> None: ...
```

Then build an email-specific workspace module above it:

```python
class AssistantWorkspace:
    async def project_thread(self, assistant_id: str, files: list[ProjectedFile]) -> None: ...
    async def project_inbound_attachments(self, run_id: str, files: list[ProjectedFile]) -> None: ...
    async def read_outbound_attachment(self, run_id: str, path: str) -> bytes: ...
    async def assert_agent_write_allowed(self, path: str) -> None: ...
```

The Docker adapter would implement `SandboxEnvironment`. The email runtime would
mostly talk to `AssistantWorkspace`.

### Benefit

This creates a deeper module boundary. Docker path handling, tar upload,
timeouts, binary reads, and shell behavior become generic sandbox concerns.
Email-specific projection and policy live in one place above that.

It also makes future sandbox providers easier to evaluate. A Daytona, E2B,
local, or Firecracker-backed adapter would only need to implement the generic
environment, not reimplement email semantics.

### Scope

Medium to large.

Likely touched areas:

- `src/email_agent/sandbox/`
- `src/email_agent/models/sandbox.py`
- `src/email_agent/runtime/assistant_runtime.py`
- `src/email_agent/agent/assistant_agent.py`
- sandbox unit and integration tests

This can be staged by first adding the lower interface behind the existing
`AssistantSandbox`, then gradually moving projection and tool logic upward.

## 2. Separate Runtime Workspace Operations From Model-Visible Tools

### Before

The runtime and the model-facing tools both flow through sandbox-like
operations. The agent uses `read`, `write`, `edit`, `bash`, and `attach_file`.
The runtime also stages emails and reads attachments from the sandbox.

These are related but not the same kind of work:

- Runtime plumbing is trusted and should not necessarily appear in the agent
  transcript.
- Model-visible tool calls are part of the agent trace and should be recorded
  as behavior.

### After Sketch

Make the distinction explicit:

```python
class WorkspaceRuntime:
    async def stage_inputs(...) -> None: ...
    async def read_output_bytes(...) -> bytes: ...
    async def cleanup_run(...) -> None: ...


class AgentToolset:
    async def read(path: str, *, offset: int | None = None, limit: int | None = None) -> str: ...
    async def write(path: str, content: str) -> str: ...
    async def edit(path: str, old: str, new: str) -> str: ...
    async def bash(command: str, *, timeout_s: int | None = None) -> str: ...
    async def attach_file(path: str, filename: str | None = None) -> str: ...
```

The runtime uses `WorkspaceRuntime`. PydanticAI tools delegate to `AgentToolset`.
Both can share the same underlying `SandboxEnvironment`.

### Benefit

Cleaner traces and cleaner tests. We can reason about which operations were
performed by trusted orchestration code and which operations were chosen by the
model.

It also avoids forcing runtime plumbing into a `ToolCall` shape just because
that is the current sandbox API.

### Scope

Medium.

Likely touched areas:

- `src/email_agent/agent/assistant_agent.py`
- `src/email_agent/runtime/assistant_runtime.py`
- `src/email_agent/sandbox/`
- `src/email_agent/domain/run_recorder.py`
- run-step tests and admin trace tests

This can be introduced without changing behavior by wrapping the existing
`run_tool` calls first.

## 3. Introduce A Run Session Module

### Before

The agent run is currently spread across several places:

- `AssistantRuntime` loads DB rows, checks budget, projects files, recalls
  memory, assembles the prompt, calls the agent, builds the reply, sends it,
  and records the result.
- `AssistantAgent` owns tool registration and extracts PydanticAI messages into
  `RunStepRecord`s.
- `RunRecorder` persists completion/failure details.
- Memory recall snapshots are persisted directly from runtime code.

This works, but the concept "what did this agent run see and do?" does not have
one clear owner.

### After Sketch

Introduce a run-session object that owns the agent-facing execution envelope:

```python
class AgentRunSession:
    scope: AssistantScope
    run_id: str
    thread_id: str

    async def prepare_context(self) -> AgentPromptContext: ...
    async def run_agent(self) -> AgentResult: ...
    async def collect_outputs(self) -> AgentOutputs: ...
    async def build_trace(self) -> list[RunStepRecord]: ...
```

`AssistantRuntime.execute_run` would become more orchestration-shaped:

```python
async def execute_run(run_id: str) -> RunOutcome:
    loaded = await load_run(run_id)
    await budget_gate(loaded.scope)

    session = await run_session_factory.create(loaded)
    result = await session.run_agent()
    outputs = await session.collect_outputs()

    envelope = reply_builder.build(...)
    sent = await email_provider.send_reply(envelope)
    await recorder.record_completion(...)
```

### Benefit

This gives a home to prompt context, memory injected into the run, tool trace
normalization, usage aggregation, and final outputs. The runtime becomes easier
to scan because it coordinates domain steps instead of owning every detail.

It also gives the admin UI and tests a stable concept to inspect.

### Scope

Medium to large.

Likely touched areas:

- `src/email_agent/runtime/assistant_runtime.py`
- `src/email_agent/agent/assistant_agent.py`
- `src/email_agent/domain/run_recorder.py`
- `src/email_agent/models/agent.py`
- runtime tests
- admin run-detail tests if trace shape changes

This should be done after the workspace/tool boundary is clearer.

## 4. Move Prompt And Context Assembly Into A Dedicated Module

### Before

The prompt is assembled inline in `AssistantRuntime.execute_run`. It combines:

- current inbound email path
- instructions to read the file
- final response rules
- workspace rules
- memory recall block
- tool guidance

As more context is added, this method will continue to grow.

### After Sketch

Create a dedicated context assembler:

```python
class RunContextAssembler:
    async def build(
        self,
        *,
        scope: AssistantScope,
        run_id: str,
        thread: EmailThread,
        inbound: NormalizedInboundEmail,
        projection: WorkspaceProjection,
        recalled_memory: list[Memory],
    ) -> AgentPromptContext: ...
```

The returned object can be explicit:

```python
@dataclass(frozen=True)
class AgentPromptContext:
    system_prompt: str
    user_prompt: str
    recalled_memory: list[Memory]
    current_message_path: str
    workspace_rules: list[str]
```

### Benefit

Prompt construction becomes directly testable. It also becomes easier to evolve
the agent contract without making `AssistantRuntime` the dumping ground for
every instruction.

This is a good place to encode future policies like approval mode, sender
preferences, current date, output schema, or memory visibility.

### Scope

Small to medium.

Likely touched areas:

- `src/email_agent/runtime/assistant_runtime.py`
- new module under `src/email_agent/domain/` or `src/email_agent/agent/`
- runtime tests
- memory recall tests if prompt snapshots are asserted

This is a good early refactor because it can preserve behavior exactly.

## 5. Use Connector-Style Adapters For Sandbox Providers

### Before

`DockerSandbox` is both provider adapter and product policy:

- creates/reuses containers
- bind-mounts workspace
- uploads files through tar archives
- executes shell commands
- enforces memory and CPU limits
- enforces `/workspace/emails` read-only behavior
- implements model-facing tool dispatch
- reads generated files back out

### After Sketch

Split provider mechanics from workspace policy:

```python
class DockerEnvironmentAdapter(SandboxEnvironment):
    ...


class SandboxLifecycleManager:
    async def get_environment(self, assistant_id: str) -> SandboxEnvironment: ...
    async def reset(self, assistant_id: str) -> None: ...


class AssistantWorkspace:
    def __init__(self, env: SandboxEnvironment, policy: WorkspacePolicy): ...
```

Provider adapters become thin connector-style modules. They adapt a sandbox
provider to the common environment interface and avoid owning product semantics.

### Benefit

Provider swaps become realistic. The project can experiment with a different
sandbox implementation without disturbing the email agent behavior.

This also makes `DockerSandbox` less brittle: lifecycle bugs, tar bugs, command
bugs, and policy bugs would stop being bundled into one class.

### Scope

Large if done fully, medium if staged.

Likely touched areas:

- `src/email_agent/sandbox/docker.py`
- `src/email_agent/sandbox/inmemory.py`
- `src/email_agent/composition.py`
- integration tests around Docker
- config settings for sandbox lifecycle and resources

Best staged after defining `SandboxEnvironment`.

## 6. Prefer Call-Scoped Runtime Options Over Hidden Global State

### Before

Some run behavior is configured globally through settings or composition:

- model factory
- run timeout
- bash timeout
- sandbox limits
- dry-run email provider wiring

Tests can override pieces, but the run-level contract is not explicit.

### After Sketch

Introduce a run options object:

```python
@dataclass(frozen=True)
class AgentRunOptions:
    model_name: str
    run_timeout_s: float | None
    bash_timeout_s: int
    memory_mode: Literal["recall", "off"]
    approval_mode: Literal["auto_send", "require_review"]
    tool_policy: ToolPolicy
    output_mode: Literal["plain_text", "structured_reply"]
```

`AssistantRuntime` or a factory can resolve this from settings, assistant scope,
budget state, and operator controls. The agent/session layer receives it
explicitly.

### Benefit

Run behavior becomes easier to reason about and easier to test. A test can run
one execution with memory off, approval required, or a smaller timeout without
mutating broad process-level wiring.

This pattern also helps if assistants eventually need different policies.

### Scope

Small to medium initially.

Likely touched areas:

- `src/email_agent/runtime/assistant_runtime.py`
- `src/email_agent/config.py`
- `src/email_agent/models/assistant.py`
- tests that construct runtimes directly

The first version can be a simple dataclass populated from existing settings.

## 7. Consider Structured Finalization Instead Of Plain Text Output

### Before

The agent's final PydanticAI output is a plain string. The runtime treats that
string as the reply body. Attachments are side effects collected through the
`attach_file` tool.

This is simple, but it leaves future intent implicit:

- Should this reply require human review?
- Did the agent intentionally decline to reply?
- Are there memory notes worth curating?
- Which generated files were intended as attachments?
- Did the agent satisfy the user's request or produce a fallback?

### After Sketch

Move toward a structured final answer:

```python
@dataclass(frozen=True)
class FinalReply:
    body_text: str
    attachments: list[PendingAttachment]
    needs_human_review: bool
    confidence: Literal["low", "medium", "high"]
    memory_notes: list[str]
```

This could be implemented either as PydanticAI structured output or as a
model-facing `finish_reply` tool:

```text
finish_reply(
  body_text,
  attachments,
  needs_human_review,
  confidence,
  memory_notes
)
```

### Benefit

The runtime stops inferring important intent from plain text and tool side
effects. It also creates a natural place for approval workflows, richer admin
inspection, and better memory curation.

### Scope

Medium.

Likely touched areas:

- `src/email_agent/agent/assistant_agent.py`
- `src/email_agent/models/agent.py`
- `src/email_agent/runtime/assistant_runtime.py`
- `src/email_agent/domain/reply_envelope.py`
- admin run detail templates
- agent tests

This should wait until the current plain-text path is stable. It changes the
agent contract.

## 8. Use Child Tasks Carefully, Mostly Behind Trusted Orchestration

### Before

One agent run handles the whole email response. It can inspect files, run bash,
search memory, and produce a reply.

### After Sketch

Introduce internal sub-runs only where they reduce complexity:

- summarize a long thread
- classify whether the email requires action
- inspect attachments
- verify a generated artifact
- draft a reply separately from final approval

These should initially be runtime-orchestrated, not freely exposed as a model
tool.

```python
thread_summary = await subtask_runner.summarize_thread(...)
draft = await subtask_runner.draft_reply(..., context=thread_summary)
verification = await subtask_runner.verify_artifacts(...)
```

### Benefit

Subtasks can have smaller prompts, clearer outputs, and separate tests. They
can also use cheaper models or stricter schemas.

The caution is that unconstrained delegation increases runtime complexity and
cost. For this project, child tasks should solve specific email-agent problems,
not become a generic agentic feature for its own sake.

### Scope

Medium to large depending on depth.

Likely touched areas:

- `src/email_agent/agent/`
- `src/email_agent/runtime/`
- usage and cost recording
- run-step persistence
- admin trace UI

This is not a near-term foundation refactor. It becomes attractive when a
specific workflow is too large for one agent turn.

## Suggested Refactor Order

1. Extract prompt/context assembly into a dedicated module.
2. Add a generic sandbox environment interface behind the current sandbox.
3. Separate runtime workspace operations from model-visible tools.
4. Split Docker provider mechanics from assistant workspace policy.
5. Introduce a run-session module once the above boundaries are clearer.
6. Consider structured finalization after the reply workflow stabilizes.
7. Add child tasks only for specific workflows that justify the extra machinery.

The overall direction is to make the architecture deeper around the concepts
that are likely to change: sandbox providers, workspace policy, agent context,
run transcripts, and final reply semantics.
