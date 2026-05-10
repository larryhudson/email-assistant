# Slice 4 — Sandbox Implementation Plan

**Goal:** A real per-assistant Docker sandbox that the agent can read/write/edit/bash inside, plus the host-side projector that lays out the email thread as files at `/workspace/emails/`. After this slice, `AssistantSandbox` has a production adapter alongside `InMemorySandbox`.

**Architecture:** Two new pieces.

1. `domain/workspace_projector.py` — `EmailWorkspaceProjector` (host-side). Given an `EmailThread` + ordered `EmailMessage`s + `EmailAttachmentRow`s, builds a deterministic directory tree at `data/run_inputs/<run_id>/emails/<thread-id>/` (`thread.md`, `NNNN-YYYY-MM-DD-from-<who>.md` per email, `attachments/NNNN-<filename>`). The directory is wiped and rebuilt every run. The runtime bind-mounts it read-only into the container at `/workspace/emails/`.
2. `sandbox/docker.py` — `DockerSandbox` adapter. Long-lived per-assistant container, lazy start. Bind-mounts: `data/sandboxes/<assistant_id>/workspace/` rw at `/workspace`, `data/run_inputs/<run_id>/emails/` ro at `/workspace/emails/`, `data/run_inputs/<run_id>/attachments/` rw at `/workspace/attachments/`. Resource limits, per-tool + per-run timeouts. Tool dispatch is `docker exec` shelling into helper scripts inside the container.

The projector is the seam between the persisted DB rows and what the agent sees on disk. `DockerSandbox` is the seam between `ToolCall` and the container.

**Idle shutdown** is intentionally deferred — needs a background sweeper that doesn't fit cleanly into the per-run flow. Tracked as an open item.

**Tech additions:**
- `docker` (Python SDK, `>=7`) as a runtime dep.
- Build script + `Dockerfile.sandbox` for the base image (`python:3.13-slim` + `curl git ripgrep jq poppler-utils`).
- A `requires_docker` pytest marker that skips when the daemon is unreachable (so the unit suite still runs without docker installed).

**Out of scope (slice 5+):** AssistantAgent, AssistantRuntime.execute_run wiring, `attach_file` plumbing back into the outbound envelope, idle-shutdown sweeper, allowlist egress.

---

## File Structure

**Create:**
- `src/email_agent/domain/workspace_projector.py` — `EmailWorkspaceProjector`, `ProjectionResult`.
- `src/email_agent/sandbox/docker.py` — `DockerSandbox` + helpers (`_container_name`, `_run_exec`, etc.).
- `docker/sandbox/Dockerfile` — base image definition.
- `tests/unit/domain/test_workspace_projector.py` — pure-function tests against a tmp_path.
- `tests/integration/test_docker_sandbox.py` — real docker tests, marked `integration` + `requires_docker`.
- `tests/integration/conftest.py` — adds `requires_docker` marker + skip helper.

**Modify:**
- `pyproject.toml` — add `docker` dep + register the `requires_docker` marker.
- `src/email_agent/config.py` — already has `sandbox_*` settings; add `run_inputs_root` if missing (it already exists, double-check).
- `docker-compose.yml` — bind-mount `./data` into the worker at `/Users/larryhudson/github.com/larryhudson/email-assistant/data` so host bind-mount paths line up. (Verify whether this is already done from earlier slices; if so, no-op.)

---

## Conventions

- TDD red-green-refactor, one failing test at a time, commit per cycle.
- Projector tests: pure tmp_path, no docker, sub-second.
- DockerSandbox tests: real docker. Each test starts a fresh container with a unique assistant_id, tears it down in a finalizer. Skipped via `requires_docker` when `docker.from_env().ping()` raises.
- Container naming: `email-agent-sandbox-<assistant_id>`. Image tag: `email-agent-sandbox:slice4` (or whatever `Settings.sandbox_image` reads).
- IDs in fixtures: `uuid.uuid4().hex[:8]` with prefix.
- Commit subjects follow `<type>(<scope>): <subject>`.

---

## Task 0: Add docker SDK dep + Dockerfile

- [ ] `uv add docker`. Verify `import docker; docker.__version__`.
- [ ] Write `docker/sandbox/Dockerfile`:
  ```dockerfile
  FROM python:3.13-slim
  RUN apt-get update \
      && apt-get install -y --no-install-recommends \
           curl git ripgrep jq poppler-utils \
      && rm -rf /var/lib/apt/lists/*
  WORKDIR /workspace
  CMD ["sleep", "infinity"]
  ```
- [ ] Build it manually once: `docker build -t email-agent-sandbox:slice4 docker/sandbox/`.
- [ ] Commit `chore(sandbox): add base image dockerfile + docker SDK dep`.

---

## Task 1: Register `requires_docker` marker + skip helper

**Files:** `pyproject.toml`, `tests/integration/conftest.py`.

- [ ] Add `"requires_docker: requires a reachable docker daemon"` to `[tool.pytest.ini_options].markers`.
- [ ] In `tests/integration/conftest.py`, add a fixture or `pytest_collection_modifyitems` hook that skips items marked `requires_docker` when `docker.from_env().ping()` raises. Cache the probe.
- [ ] Add a single throwaway integration test marked `requires_docker` that asserts the daemon responds; run `uv run pytest -m integration` to confirm it skips/runs as expected.
- [ ] Commit `test: add requires_docker marker that skips when daemon unreachable`.

---

## Task 2: `EmailWorkspaceProjector` — projects a single inbound thread

**Files:** `src/email_agent/domain/workspace_projector.py`, `tests/unit/domain/test_workspace_projector.py`.

- [ ] **Step 1 (red):** Test seeds an in-memory `EmailThread` + two ordered `EmailMessage`s (one inbound + one outbound) + a single `EmailAttachmentRow` (the attachment file already on disk under tmp_path). Calls `EmailWorkspaceProjector(run_inputs_root=tmp_path).project(thread, messages, attachments, current_message_id)` and asserts:
  - returned `ProjectionResult.current_message_path == "emails/<thread-id>/0001-…-from-mum-at-example-com.md"`
  - `tmp_path/<run_id>/emails/<thread-id>/thread.md` exists with subject + participants
  - one `.md` per message, ordered `0001`, `0002`
  - markdown frontmatter contains `from`, `to`, `date`, `subject`, `message_id`
  - `tmp_path/<run_id>/emails/<thread-id>/attachments/0001-<filename>` is a copy of the source bytes
- [ ] **Step 2 (green):** Implement `EmailWorkspaceProjector`:
  ```python
  @dataclass(frozen=True)
  class ProjectionResult:
      run_inputs_dir: Path        # absolute host path to the run's inputs root
      emails_dir: Path            # absolute host path to /workspace/emails/<thread>
      current_message_path: str   # sandbox-relative, e.g. "emails/<thread>/0001-….md"
  ```
  Steps inside `.project(...)`: rmtree the per-run dir if it exists; recreate; write `thread.md`; for each message write `NNNN-<date>-from-<sanitized-from>.md`; copy attachments. Sort messages by `created_at` then `id` for determinism.
- [ ] Commit `feat(domain): EmailWorkspaceProjector lays out thread for sandbox`.

---

## Task 3: Projector — wipes the per-run directory before regenerating

- [ ] **Step 1 (red):** Test seeds the run's projection dir with stale junk (`emails/dead-thread/zzz.md`), runs `.project(...)`, and asserts the junk is gone.
- [ ] **Step 2 (green):** Confirm the rmtree-before-recreate logic from Task 2 already covers it; if not, add it.
- [ ] Commit `test(domain): projector wipes stale run-input dirs before regenerating`.

---

## Task 4: Projector — markdown frontmatter + filename sanitization

- [ ] **Step 1 (red):** Test passes a message whose `from_email` is `Mum O'Connor <mum.oconnor@example.co.uk>` and subject contains slashes/quotes. Assert the filename matches `^[0-9]{4}-\d{4}-\d{2}-\d{2}-from-mum-oconnor-at-example-co-uk\.md$` and that the markdown frontmatter quotes the subject correctly.
- [ ] **Step 2 (green):** Add `_sanitize_email_for_filename` helper (lowercase, replace `[^a-z0-9]+` with `-`, strip trailing dashes). Frontmatter via plain triple-dashes block, escape with `json.dumps(value)` for the subject only.
- [ ] Commit `feat(domain): sanitize sender + subject in projected filenames`.

---

## Task 5: `DockerSandbox.ensure_started` — starts a container with bind mounts

**Files:** `src/email_agent/sandbox/docker.py`, `tests/integration/test_docker_sandbox.py`.

- [ ] **Step 1 (red):** Integration test marked `integration` + `requires_docker`. Constructs `DockerSandbox(client=docker.from_env(), settings=...)` pointed at a tmp `sandbox_data_root`. Calls `ensure_started("a-1")`, asserts:
  - container `email-agent-sandbox-a-1` is running (`client.containers.get(...).status == "running"`)
  - `/workspace` exists inside the container (`exec_run("ls /workspace")` returns 0)
  - resource limits applied (`HostConfig.NanoCPUs == 1_000_000_000`, `Memory == 512 * 1024 * 1024`)
  - second call is a no-op (still running, not recreated; same container ID)
- [ ] **Step 2 (green):** Implement `ensure_started`:
  - look up container by name; if exists + running, return
  - if exists + stopped, `start()`
  - else `client.containers.run(image=..., name=..., detach=True, command=["sleep", "infinity"], volumes={...}, mem_limit="512m", nano_cpus=1_000_000_000, network_mode="bridge", read_only=False, tmpfs={"/tmp": ""}, working_dir="/workspace")`
  - `mkdir -p data/sandboxes/<assistant_id>/workspace` on the host before mounting
- [ ] Add a fixture that tears down the test container in a finalizer (`container.remove(force=True)`).
- [ ] Commit `feat(sandbox): DockerSandbox.ensure_started`.

---

## Task 6: `DockerSandbox.project_emails` + `project_attachments`

- [ ] **Step 1 (red):** Test runs `ensure_started` then `project_emails("a-1", [ProjectedFile(path="emails/t-1/thread.md", content=b"hi")])`. Asserts the file appears at `/workspace/emails/t-1/thread.md` inside the container with the right bytes. Then `project_attachments("a-1", "r-1", [ProjectedFile(path="report.pdf", content=b"%PDF")])` puts it at `/workspace/attachments/r-1/report.pdf`.
- [ ] **Step 2 (green):** Two strategies — bind-mount the run's `data/run_inputs/<run_id>/emails/` into `/workspace/emails` read-only on `ensure_started`, OR write directly into the container's `/workspace` via `put_archive`. Pick the bind-mount path for emails (matches design) and `put_archive` for attachments (per-run write area).
- [ ] Commit `feat(sandbox): DockerSandbox project_emails + project_attachments`.

---

## Task 7: `DockerSandbox.run_tool` — `read`

- [ ] **Step 1 (red):** Test projects a small file and runs `await sandbox.run_tool("a-1", "r-1", ToolCall(kind="read", path="emails/t-1/thread.md"))`. Asserts `result.ok is True` and `result.output == "hi"`.
- [ ] **Step 2 (green):** Implement `run_tool` for `read` via `container.exec_run(["cat", path], demux=False)`, decode utf-8, surface non-zero exit as `ok=False, error=stderr`. Resolve `path` against `/workspace`.
- [ ] Commit `feat(sandbox): DockerSandbox read tool`.

---

## Task 8: `run_tool` — `write` + `edit` (with /workspace/emails/ guard)

- [ ] **Step 1 (red):** Two tests:
  - `write` to `notes/draft.md` succeeds; `read` confirms content.
  - `write` to `emails/t-1/thread.md` fails with `ToolResult(ok=False, error~="read-only")` and the file is unchanged.
- [ ] **Step 2 (green):** Implement `write` (`put_archive` of a single tar entry) and `edit` (read → str.replace → write). Both check `path.startswith("emails/")` (or absolute equivalent) and refuse with a typed error before touching the container.
- [ ] **Step 3 (red):** Test `edit` for "old not found" returns `ok=False, error~="not found"` and leaves the file alone.
- [ ] **Step 4 (green):** Implement that branch.
- [ ] Commit `feat(sandbox): DockerSandbox write + edit tools with read-only emails guard`.

---

## Task 9: `run_tool` — `bash` with per-tool timeout

- [ ] **Step 1 (red):** Two tests:
  - `bash("echo hello && exit 0")` returns `ok=True, output=BashResult(exit_code=0, stdout="hello\n", stderr="", duration_ms>0)`.
  - `bash("sleep 30")` with `bash_timeout_seconds=2` returns `ok=False, error~="timeout"` within ~3s wall-clock.
- [ ] **Step 2 (green):** Implement via `container.exec_run` with `stream=False`, but wrap in `asyncio.wait_for` against a thread executor since the docker SDK is sync. On timeout, kill the exec process (`docker exec --kill` is awkward; alternative: spawn the command via `bash -c "timeout <n>s <cmd>"` using GNU timeout from the base image).
- [ ] Commit `feat(sandbox): DockerSandbox bash tool with timeout`.

---

## Task 10: `run_tool` — `attach_file` records the pending attachment

- [ ] **Step 1 (red):** Test calls `attach_file` for an existing `/workspace/notes/report.pdf`; asserts the result is `ok=True` and `read_attachment_out("a-1", "r-1", "notes/report.pdf")` returns the file's bytes.
- [ ] **Step 2 (green):** `attach_file` validates the path exists in the container (`exec_run(["test", "-f", path])`); records nothing internally — the runtime owns `pending_attachments`. `read_attachment_out` uses `container.get_archive` to pull a single file.
- [ ] Commit `feat(sandbox): DockerSandbox attach_file + read_attachment_out`.

---

## Task 11: `DockerSandbox.reset` — wipes /workspace and recreates the container

- [ ] **Step 1 (red):** Test writes a file, calls `reset("a-1")`, then `ensure_started("a-1")` again, then `read` returns "file not found".
- [ ] **Step 2 (green):** Stop + remove the container; rmtree the host workspace dir; the next `ensure_started` recreates everything.
- [ ] Commit `feat(sandbox): DockerSandbox reset`.

---

## Task 12: Re-run full suite + lint + types

- [ ] `uv run pytest -q` (unit only)
- [ ] `uv run pytest -m integration` (requires docker — verify locally)
- [ ] `uv run ruff check && uv run ruff format --check && uv run ty check`

---

## Done when

- `EmailWorkspaceProjector` produces a deterministic, wipe-and-regenerate file tree from a thread + messages + attachments. Filenames are sanitised; markdown frontmatter is well-formed.
- `DockerSandbox` starts/stops a per-assistant container with the right mounts, resource limits, and read-only `/workspace/emails/`.
- `read`, `write`, `edit`, `bash`, `attach_file`, `read_attachment_out`, `reset` all work against a real container, with the read-only guard, per-tool timeout, and a typed-error contract that mirrors `InMemorySandbox`.
- All slice-4 tests + lint + types green; integration tests skipped cleanly when docker is absent.

Wiring (`AssistantRuntime.execute_run` calling projector + sandbox + agent) is slice 5. Idle-shutdown sweeper is deferred.
