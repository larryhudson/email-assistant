# Slice 6 — Cognee Memory Adapter Implementation Plan

**Goal:** Replace the placeholder `InMemoryMemoryAdapter` in production with a real `CogneeMemoryAdapter` that satisfies `MemoryPort`, isolates per-assistant memory on disk, and serializes Cognee's module-global config under a process-wide `asyncio.Lock`.

After this slice:
- Production composition wires `CogneeMemoryAdapter` instead of `InMemoryMemoryAdapter`.
- Recall queries actually return cross-thread durable memory the agent has accumulated for that assistant.
- The unit suite still uses `InMemoryMemoryAdapter` — fast, no network, no embedding API key.
- A single integration test (gated by `EMAIL_AGENT_E2E=1`) round-trips `record_turn` → `recall` against real Cognee with a tmp data root.

**Architecture:** One new adapter, one config-swap helper, one process-wide lock. The mapping the adapter implements:

| `MemoryPort` method | Cognee call (under the lock, with that assistant's config) |
| --- | --- |
| `record_turn(assistant_id, thread_id, role, content)` | `await cognee.remember(f"[{role}] {content}", session_id=thread_id)` |
| `recall(assistant_id, thread_id, query)` | `await cognee.recall(query, session_id=thread_id)` → wrap in `MemoryContext` |
| `search(assistant_id, query)` | `await cognee.recall(query)` (no `session_id` — falls through to the graph) |
| `delete_assistant(assistant_id)` | `await cognee.forget(...)` if a per-assistant dataset name is exposed; otherwise `shutil.rmtree(per_assistant_root)` under the lock |

Cognee's `data_root_directory` and `system_root_directory` are module-global. Every public adapter method takes the lock, switches both directories to `data/cognee/<assistant_id>/{data,system}`, runs the cognee call, and releases. `curate_memory` jobs and admin reads share the same lock.

The README confirmed the current top-level API is `cognee.remember`, `cognee.recall`, `cognee.forget` (not `add`+`cognify`+`search` as the older design doc text implied). The design doc's mention of `@cognee.agent_memory` is a *separate* integration point for the agent's tool loop and is **not** part of this slice — that's a follow-up. This slice is just the `MemoryPort` adapter.

## Out of scope

- `@cognee.agent_memory` decorator on the agent loop (separate change, doesn't touch the port).
- Procrastinate `curate_memory` job (slice 7 — this slice exposes the right adapter shape so slice 7 can call it).
- Re-running every existing test against `CogneeMemoryAdapter`. The contract is enforced by the existing `InMemoryMemoryAdapter` tests + one integration round-trip.

## File structure

**Create:**
- `src/email_agent/memory/cognee.py` — `CogneeMemoryAdapter` + `_with_assistant_config` async context manager.
- `tests/integration/test_cognee_memory_adapter.py` — gated by `EMAIL_AGENT_E2E=1`, real Cognee, tmp data root.

**Modify:**
- `pyproject.toml` — `uv add cognee`.
- `src/email_agent/composition.py` (or wherever production wiring lives) — swap `InMemoryMemoryAdapter` for `CogneeMemoryAdapter` in the prod path, keep in-memory for tests.

## Tasks (TDD red-green-refactor)

### Task 0 — Add cognee dep

- [ ] `uv add cognee`
- [ ] `uv run python -c "import cognee; print(cognee.__version__)"`
- [ ] Commit `chore(deps): add cognee for memory adapter`

### Task 1 — Per-assistant config swap helper

Red: a unit test that calls the helper with two different `assistant_id`s and asserts `cognee.config` reports the right `data_root_directory` *during* the `async with`, and that the helper raises if anyone tries to enter without the lock held.

Green: minimal `_with_assistant_config(lock, root, assistant_id)` async context manager.

### Task 2 — `record_turn` round-trips through real Cognee (integration, gated)

Red: write a `pytest.mark.skipif(env not set)` integration test: instantiate `CogneeMemoryAdapter(data_root=tmp_path)`, `record_turn("a-1", "t-1", "user", "I love sourdough")`, then `recall("a-1", "t-1", "sourdough")`, assert the returned `MemoryContext.memories` is non-empty and at least one mentions sourdough.

Green: implement `record_turn` + `recall` against `cognee.remember` / `cognee.recall` under the lock.

### Task 3 — Per-assistant isolation (integration, gated)

Red: store secret-A under `assistant_id="a-1"` and secret-B under `"a-2"`, then `recall` and `search` from `"a-2"` and assert secret-A is never returned.

Green: confirm config swap actually puts them in different on-disk roots; if `cognee.recall` leaks across roots due to in-process caches, document and fix (likely needs `cognee.disconnect()` or equivalent between swaps — discover empirically).

### Task 4 — `delete_assistant` wipes that assistant only

Red: store under `a-1` and `a-2`, call `delete_assistant("a-1")`, assert `search("a-1", ...)` is empty and `search("a-2", ...)` still finds its entry.

Green: prefer `cognee.forget(...)` if there's a dataset-scoped form; fall back to `shutil.rmtree(per_assistant_root)` under the lock.

### Task 5a — `remember` agent tool

The agent currently has `memory_search` (read-side) but no write-side counterpart. Add a `remember(content: str)` PydanticAI tool on `AssistantAgent` that calls `ctx.deps.memory.record_turn(assistant_id, thread_id, role="agent_memory", content=content)`. Lets the agent deliberately persist a fact mid-run.

Red: a `FunctionModel`-driven unit test that scripts the agent to call `remember("Mum prefers detailed explanations")`, then asserts the in-memory adapter has that content stored under `(assistant_id, thread_id)`.

Green: add the tool in `assistant_agent.py` next to `memory_search`.

### Task 5b — Wire into production composition

Red: a unit test asserting `composition.build_production_runtime(...)` (or the equivalent helper) returns a runtime whose `MemoryPort` is a `CogneeMemoryAdapter` instance with the configured data root.

Green: swap the wiring; keep tests on `InMemoryMemoryAdapter`.

### Task 6 — Refactor + smoke

- [ ] `uv run ruff format && uv run ruff check && uv run ty check`
- [ ] `uv run pytest tests/unit` (must stay green)
- [ ] `EMAIL_AGENT_E2E=1 uv run pytest tests/integration/test_cognee_memory_adapter.py` if a Cognee-compatible LLM/embedding key is available; otherwise document the env vars needed in the test docstring.
- [ ] Commit `feat(memory): add CogneeMemoryAdapter and wire into prod composition`

## Conventions reminder

- One failing test at a time, behaviour-driven (no shape-only tests).
- Test helpers use explicit kwargs, no `kw.pop`.
- `ty: ignore[...]` (NOT `type: ignore`) when suppressing.
- Don't bypass pre-commit hooks.
