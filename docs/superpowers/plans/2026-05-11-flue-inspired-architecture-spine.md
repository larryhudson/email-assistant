# Flue-Inspired Architecture Spine — TDD Implementation Plan

**Goal:** Refactor the agent execution path toward a cleaner architecture
without changing user-visible behavior. The near-term spine is:

```text
RunContextAssembler
  -> SandboxEnvironment
  -> AssistantWorkspace
  -> AgentToolset
  -> DockerEnvironmentAdapter
```

This plan intentionally avoids adopting Flue itself. We are borrowing its
architectural pattern: a deeper environment abstraction, a separate workspace
policy layer, and a cleaner boundary between trusted runtime operations and
model-visible tools.

## Desired End State

- `AssistantRuntime` coordinates the run; it does not assemble prompts inline
  or know low-level sandbox mechanics.
- `RunContextAssembler` owns the prompt/context contract for an email agent
  run.
- `SandboxEnvironment` is a generic filesystem/shell interface.
- `AssistantWorkspace` owns email-specific staging, read-only policy, and
  output extraction.
- `AgentToolset` owns model-visible tool behavior and delegates to
  `AssistantWorkspace`.
- Existing `AssistantSandbox` behavior is preserved until the new spine is
  complete enough to replace it.

## Ground Rules

- Red-green-refactor, one behavior at a time.
- Keep public behavior stable until a task explicitly changes a contract.
- Prefer adapter shims over big-bang rewrites.
- Add tests at the new boundary before moving production code behind it.
- Keep Docker integration tests marked `integration` / `requires_docker`.
- Do not combine architectural extraction with unrelated feature work.

## Non-Goals

- No Flue dependency.
- No TypeScript sidecar.
- No child tasks yet.
- No structured final reply yet.
- No new sandbox provider yet.
- No admin UI redesign.

---

## Phase 1: Extract `RunContextAssembler`

**Purpose:** Move prompt/context assembly out of `AssistantRuntime` first. This
is low risk and creates a clearer place for later workspace/tool instructions.

### Target Shape

Create:

- `src/email_agent/agent/run_context.py`
- `tests/unit/agent/test_run_context.py`

Sketch:

```python
@dataclass(frozen=True)
class AgentPromptContext:
    prompt: str
    recalled_memory: list[Memory]
    current_message_path: str


class RunContextAssembler:
    def build(
        self,
        *,
        current_message_path: str,
        memories: list[Memory],
    ) -> AgentPromptContext: ...
```

Keep the first version intentionally narrow: reproduce the exact prompt that
`AssistantRuntime.execute_run` currently builds.

### TDD Cycles

#### 1.1 Red: Captures Current Prompt Contract

Write a unit test that calls `RunContextAssembler.build(...)` with:

- `current_message_path="emails/t-1/0001.md"`
- no memories

Assert the prompt includes:

- the current message path
- instruction to read via `read`
- final response becomes reply email body
- do not write reply to disk
- do not modify `emails/`
- use `memory_search`
- use `attach_file` only for real artifacts

Expected red: module/class does not exist.

#### 1.2 Green: Move Prompt Text Into Assembler

Implement the smallest assembler that passes the test. Do not wire it into
runtime yet.

#### 1.3 Red: Memory Block Is Included

Add a test with two `Memory` instances and assert the prompt includes a
`Recalled memory:` block with each memory's content.

#### 1.4 Green: Add Memory Formatting

Add memory formatting that matches existing runtime behavior.

#### 1.5 Refactor: Wire Runtime Through The Assembler

Modify `AssistantRuntime.execute_run` to call `RunContextAssembler`.

Verification:

```bash
uv run pytest tests/unit/agent/test_run_context.py tests/unit/runtime/test_execute_run.py tests/unit/runtime/test_runtime.py -q
```

Expected touched files:

- `src/email_agent/agent/run_context.py`
- `src/email_agent/runtime/assistant_runtime.py`
- `tests/unit/agent/test_run_context.py`
- possibly runtime tests if they assert exact prompts

---

## Phase 2: Introduce `SandboxEnvironment`

**Purpose:** Add a generic filesystem/shell boundary below the current
email-shaped sandbox API. Preserve `AssistantSandbox` while the lower layer
stabilizes.

### Target Shape

Create:

- `src/email_agent/sandbox/environment.py`
- `tests/unit/sandbox/test_environment_port.py`

Sketch:

```python
@dataclass(frozen=True)
class FileStat:
    is_file: bool
    is_dir: bool
    is_symlink: bool
    size: int
    mtime: datetime | None = None


@dataclass(frozen=True)
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


@runtime_checkable
class SandboxEnvironment(Protocol):
    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
    ) -> ShellResult: ...

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

### TDD Cycles

#### 2.1 Red: Protocol Is Runtime-Checkable

Write a small fake class in a unit test that implements the methods and assert
`isinstance(fake, SandboxEnvironment)`.

Expected red: protocol does not exist.

#### 2.2 Green: Add Port And Models

Implement the protocol and dataclasses only.

#### 2.3 Red: In-Memory Environment Basic File Ops

Create `InMemoryEnvironment` tests for:

- write/read text
- write/read bytes
- exists
- readdir
- mkdir parents
- rm force
- relative paths resolve under `/workspace`

Expected red: adapter does not exist.

#### 2.4 Green: Add `InMemoryEnvironment`

Create `src/email_agent/sandbox/inmemory_environment.py`. Keep it simple and
test-focused.

#### 2.5 Red: Exec Contract

Add a test that `exec("printf hello")` returns `ShellResult(exit_code=0,
stdout="hello", stderr="", duration_ms>=0)`.

#### 2.6 Green: Minimal Exec

For `InMemoryEnvironment`, shell out with `subprocess.run` exactly as the
current `InMemorySandbox` does. This is test-only behavior, not production
security.

Verification:

```bash
uv run pytest tests/unit/sandbox/test_environment_port.py tests/unit/sandbox/test_inmemory_environment.py -q
```

Expected touched files:

- `src/email_agent/sandbox/environment.py`
- `src/email_agent/sandbox/inmemory_environment.py`
- `src/email_agent/sandbox/__init__.py`
- `tests/unit/sandbox/test_environment_port.py`
- `tests/unit/sandbox/test_inmemory_environment.py`

---

## Phase 3: Build `AssistantWorkspace` Over `SandboxEnvironment`

**Purpose:** Move email-specific staging, attachment extraction, and read-only
workspace policy into a layer above the generic environment.

### Target Shape

Create:

- `src/email_agent/sandbox/workspace.py`
- `tests/unit/sandbox/test_assistant_workspace.py`

Sketch:

```python
class WorkspacePolicyError(Exception): ...


class AssistantWorkspace:
    def __init__(self, env: SandboxEnvironment) -> None: ...

    async def project_emails(self, files: list[ProjectedFile]) -> None: ...
    async def project_attachments(self, run_id: str, files: list[ProjectedFile]) -> None: ...
    async def read_outbound_attachment(self, path: str) -> bytes: ...
    async def assert_agent_write_allowed(self, path: str) -> None: ...
```

First implementation can use paths compatible with existing behavior:

- emails under `/workspace/emails`
- inbound attachments under `/workspace/attachments/<run_id>`
- generated files anywhere else under `/workspace`

### TDD Cycles

#### 3.1 Red: Projects Emails Into Read-Only Area

Using `InMemoryEnvironment`, assert `AssistantWorkspace.project_emails(...)`
writes files under `/workspace/emails/...` and wipes stale email files before
projecting new ones.

Expected red: workspace module does not exist.

#### 3.2 Green: Implement Email Projection

Implement with environment operations. No Docker changes yet.

#### 3.3 Red: Projects Attachments Under Run Directory

Assert `project_attachments("r-1", [ProjectedFile(path="x.pdf", ...)])` writes
`/workspace/attachments/r-1/x.pdf`.

#### 3.4 Green: Implement Attachment Projection

Add the method.

#### 3.5 Red: Enforces Agent Write Policy

Assert:

- `/workspace/emails/x.md` is rejected
- `emails/x.md` is rejected
- `/workspace/notes.md` is allowed
- `notes.md` is allowed

#### 3.6 Green: Add Policy Method

Implement path normalization and `WorkspacePolicyError`.

#### 3.7 Refactor: Reuse From Current `InMemorySandbox`

Modify `InMemorySandbox` internally to delegate projection and policy checks to
`AssistantWorkspace` where practical, while preserving the `AssistantSandbox`
public API.

Verification:

```bash
uv run pytest tests/unit/sandbox/test_assistant_workspace.py tests/unit/sandbox/test_inmemory.py -q
```

Expected touched files:

- `src/email_agent/sandbox/workspace.py`
- `src/email_agent/sandbox/inmemory.py`
- `tests/unit/sandbox/test_assistant_workspace.py`
- existing in-memory sandbox tests

---

## Phase 4: Extract `AgentToolset`

**Purpose:** Move model-visible tool behavior out of `AssistantAgent` so tool
semantics can evolve independently from PydanticAI registration.

### Target Shape

Create:

- `src/email_agent/agent/toolset.py`
- `tests/unit/agent/test_toolset.py`

Sketch:

```python
class AgentToolset:
    def __init__(
        self,
        *,
        assistant_id: str,
        run_id: str,
        workspace: AssistantWorkspace,
        memory: MemoryPort,
        pending_attachments: list[PendingAttachment],
    ) -> None: ...

    async def read(self, path: str, *, offset: int | None = None, limit: int | None = None) -> str: ...
    async def write(self, path: str, content: str) -> str: ...
    async def edit(self, path: str, old: str, new: str) -> str: ...
    async def bash(self, command: str, *, timeout_s: int | None = None) -> str: ...
    async def attach_file(self, path: str, filename: str | None = None) -> str: ...
    async def memory_search(self, query: str) -> list[Memory]: ...
```

Keep first behavior compatible with existing tool outputs. Optional improvements
like read pagination, `grep`, and `glob` should wait until this extraction is
done.

### TDD Cycles

#### 4.1 Red: Read Delegates To Workspace

Test with `InMemoryEnvironment` and `AssistantWorkspace`. Write a file, call
`toolset.read`, assert content is returned.

Expected red: toolset module does not exist.

#### 4.2 Green: Implement `read`

Implement only enough for the test.

#### 4.3 Red: Write Rejects Emails Directory

Assert `toolset.write("emails/x.md", "x")` returns the same error-style string
or raises/normalizes consistently with existing tool behavior.

Decision: for compatibility with current `AssistantAgent`, `AgentToolset`
methods should return strings suitable for the model, not raise for expected
tool failures.

#### 4.4 Green: Implement `write`

Use `AssistantWorkspace.assert_agent_write_allowed`.

#### 4.5 Red/Green: Edit

Tests:

- edits first matching occurrence
- returns error text when old string is missing
- rejects emails directory

#### 4.6 Red/Green: Bash

Test formatting:

```text
exit_code=0
stdout:
...
stderr:
...
```

Keep existing formatting.

#### 4.7 Red/Green: Attach File

Test:

- existing file appends `PendingAttachment`
- missing file returns error text
- filename defaults to basename

#### 4.8 Red/Green: Memory Search

Use an in-memory fake memory port and assert delegation by assistant id.

#### 4.9 Refactor: PydanticAI Tool Callbacks Delegate To `AgentToolset`

Update `AgentDeps` to include either:

- an `AgentToolset` directly, or
- enough dependencies to construct one inside each PydanticAI tool callback.

Prefer constructing once per run and passing it in deps if this keeps callbacks
thin.

Verification:

```bash
uv run pytest tests/unit/agent/test_toolset.py tests/unit/agent/test_assistant_agent.py tests/unit/runtime/test_execute_run.py -q
```

Expected touched files:

- `src/email_agent/agent/toolset.py`
- `src/email_agent/agent/assistant_agent.py`
- `src/email_agent/models/agent.py`
- `src/email_agent/runtime/assistant_runtime.py`
- agent/runtime tests

---

## Phase 5: Split Docker Provider Mechanics Into `DockerEnvironmentAdapter`

**Purpose:** Make Docker one implementation of `SandboxEnvironment`, then keep
`DockerSandbox` as a compatibility adapter until the old `AssistantSandbox`
surface can be retired.

### Target Shape

Create or reshape:

- `src/email_agent/sandbox/docker_environment.py`
- keep `src/email_agent/sandbox/docker.py` as a compatibility wrapper

Sketch:

```python
class DockerEnvironmentAdapter(SandboxEnvironment):
    def __init__(self, *, container: Container, bash_timeout_seconds: int) -> None: ...
    async def exec(...) -> ShellResult: ...
    async def read_text(...) -> str: ...
    async def read_bytes(...) -> bytes: ...
    async def write_text(...) -> None: ...
    async def write_bytes(...) -> None: ...
    ...


class DockerSandbox:
    async def ensure_started(self, assistant_id: str) -> None: ...
    def environment_for(self, assistant_id: str) -> SandboxEnvironment: ...
```

`DockerSandbox.run_tool(...)` can become a compatibility shim that builds an
`AssistantWorkspace` + `AgentToolset` over `environment_for(assistant_id)`.

### TDD Cycles

#### 5.1 Red: Docker Environment Reads/Writes Text

Integration test:

- start a container through existing Docker setup
- create `DockerEnvironmentAdapter`
- `write_text("/workspace/notes.md", "hello")`
- `read_text("/workspace/notes.md") == "hello"`

Expected red: adapter does not exist.

#### 5.2 Green: Implement Text Read/Write

Move or wrap existing tar helpers from `DockerSandbox`.

#### 5.3 Red/Green: Binary Read/Write

Test bytes round trip for a small binary payload.

#### 5.4 Red/Green: Exec With Timeout

Tests:

- `exec("printf hi")` returns exit code 0 and stdout
- `exec("sleep 2", timeout_s=1)` returns timeout-shaped result

Preserve existing timeout behavior where possible.

#### 5.5 Red/Green: Directory Ops

Tests:

- `mkdir(..., parents=True)`
- `readdir`
- `exists`
- `stat`
- `rm(..., recursive=True, force=True)`

#### 5.6 Refactor: `DockerSandbox` Delegates To Environment + Workspace

Keep all existing `tests/integration/test_docker_sandbox.py` passing while
internally delegating to the new adapter.

Verification:

```bash
uv run pytest tests/integration/test_docker_sandbox.py -q
```

Expected touched files:

- `src/email_agent/sandbox/docker_environment.py`
- `src/email_agent/sandbox/docker.py`
- `src/email_agent/sandbox/workspace.py`
- Docker integration tests

---

## Phase 6: Collapse Runtime Onto The New Spine

**Purpose:** Make the normal execution path use the new boundaries directly,
while preserving `AssistantSandbox` as an adapter only if still useful for
tests or compatibility.

### Target Shape

`AssistantRuntime.execute_run` should roughly read as:

```python
projection = projector.project(...)
env = await sandbox_manager.environment_for(scope.assistant_id)
workspace = AssistantWorkspace(env)
await workspace.project_emails(projected_files)

prompt_context = context_assembler.build(...)
toolset = AgentToolset(...)
agent_result = await agent.run(scope, prompt=prompt_context.prompt, deps=deps)
attachments = await workspace.collect_pending_attachments(...)
```

Exact names may differ. The key is that runtime talks to explicit context,
workspace, and toolset concepts.

### TDD Cycles

#### 6.1 Red: Runtime Uses Context Assembler

Add or adjust a runtime test with a spy/fake `RunContextAssembler` to assert
`execute_run` uses it.

This may already be covered from Phase 1. If so, skip.

#### 6.2 Red: Runtime Stages Through Workspace

Add a runtime test with a fake workspace/environment path that proves email
projection happens through `AssistantWorkspace`, not direct sandbox projection.

Expected red: runtime still calls old sandbox projection directly.

#### 6.3 Green: Inject Workspace Factory

Add a small factory dependency to `AssistantRuntime` or composition so tests can
provide a fake and production can construct from Docker/InMemory environment.

#### 6.4 Red: Agent Uses Toolset From Deps

Add a test proving registered PydanticAI tools call `AgentToolset` methods.

This may already be covered from Phase 4. If so, skip.

#### 6.5 Green: Update Runtime Deps Construction

Construct and pass the toolset explicitly.

#### 6.6 Refactor: Remove Dead Duplication

Remove duplicated prompt/tool/path logic that is now owned by:

- `RunContextAssembler`
- `AssistantWorkspace`
- `AgentToolset`
- `SandboxEnvironment`

Verification:

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration/test_docker_sandbox.py -q
```

Expected touched files:

- `src/email_agent/runtime/assistant_runtime.py`
- `src/email_agent/composition.py`
- `src/email_agent/agent/assistant_agent.py`
- `src/email_agent/models/agent.py`
- sandbox and runtime tests

---

## Phase 7: Retire Or Shrink `AssistantSandbox`

**Purpose:** Decide whether the old email-shaped sandbox port still earns its
keep after the new spine is in place.

### Options

#### Option A: Retain As Facade

Keep `AssistantSandbox` as a high-level facade for runtime use, implemented in
terms of:

- `SandboxEnvironment`
- `AssistantWorkspace`
- `AgentToolset`

This is lower risk if many tests and runtime paths still depend on it.

#### Option B: Retire It

Replace runtime dependencies with:

- `SandboxEnvironmentManager`
- `AssistantWorkspaceFactory`

Then delete `ToolCall`/`ToolResult` once no model-visible tools use that shape.

### TDD Cycles

#### 7.1 Red: No Runtime Import Of `ToolCall`

Add a lightweight import-boundary test or `rg`-based test that asserts
`runtime/` no longer imports `ToolCall`.

#### 7.2 Green: Remove Runtime Dependency

Move remaining tool-call-specific logic into `AgentToolset` or compatibility
facades.

#### 7.3 Refactor: Delete Or Deprecate Dead Models

If `ToolCall` / `ToolResult` remain only for compatibility, mark them as
legacy in comments. If no references remain, delete them and update tests.

Verification:

```bash
uv run pytest tests/unit -q
```

Expected touched files:

- `src/email_agent/sandbox/port.py`
- `src/email_agent/models/sandbox.py`
- `src/email_agent/runtime/assistant_runtime.py`
- tests that reference `ToolCall`

---

## Acceptance Criteria

The spine is done when:

- `AssistantRuntime` no longer assembles the agent prompt inline.
- Generic sandbox filesystem/shell operations are available through
  `SandboxEnvironment`.
- Email-specific workspace projection/policy lives in `AssistantWorkspace`.
- PydanticAI tool callbacks are thin registrations over `AgentToolset`.
- Docker provider mechanics live in a `DockerEnvironmentAdapter` or equivalent.
- Existing unit tests pass.
- Existing Docker sandbox integration tests pass when Docker is available.
- No user-visible email behavior intentionally changes.

## Risk Register

- **Risk:** `AgentRunSession` temptation too early.
  **Mitigation:** Do not add it in this plan. Reassess after context,
  workspace, and toolset boundaries exist.

- **Risk:** `AssistantWorkspace` and `AgentToolset` overlap.
  **Mitigation:** Workspace owns filesystem policy and staging. Toolset owns
  model-facing strings, pending attachments, and memory delegation.

- **Risk:** Docker refactor breaks integration behavior.
  **Mitigation:** Keep existing `DockerSandbox` integration tests as a
  compatibility safety net while moving internals.

- **Risk:** Test-only `InMemoryEnvironment.exec` looks production-safe.
  **Mitigation:** Document it clearly as test-only, matching current
  `InMemorySandbox` behavior.

- **Risk:** Scope creep into new tools like `grep`, `glob`, or structured
  finalization.
  **Mitigation:** Add those only after this spine lands.

## Suggested PR / Commit Slices

1. `refactor(agent): extract run context assembler`
2. `feat(sandbox): add generic sandbox environment port`
3. `feat(sandbox): add assistant workspace over environment`
4. `refactor(agent): extract model-visible toolset`
5. `refactor(sandbox): split docker environment adapter`
6. `refactor(runtime): route execution through workspace and toolset`
7. `refactor(sandbox): shrink legacy AssistantSandbox facade`

