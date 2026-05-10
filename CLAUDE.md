# email-assistant — agent guidance

## Tooling

- **Package manager:** `uv`. Add deps with `uv add <pkg>` (runtime) or `uv add --dev <pkg>` (dev). Never edit `[project.dependencies]` or `[dependency-groups].dev` by hand.
- **Lint + format:** `ruff` (`uv run ruff check`, `uv run ruff format`).
- **Type checker:** `ty` (Astral, pre-1.0). NOT mypy.
- **Tests:** `pytest` + `pytest-asyncio` (auto mode).
- **Pre-commit:** local hooks defined in `.pre-commit-config.yaml`. Ruff + ty run on every commit; `pytest tests/unit` runs on push. Never bypass hooks with `--no-verify` — fix the underlying issue.

## Type-checker ignore syntax

We use `ty`, which has its OWN comment syntax. Don't use mypy syntax.

- ✅ Correct: `# ty: ignore[missing-argument]`
- ❌ Wrong: `# type: ignore[missing-argument]` (mypy — does nothing here)
- ❌ Wrong: leaving both on the same line (redundant)

Reference: https://docs.astral.sh/ty/reference/rules/

If `ty` flags a real type problem, prefer fixing the types over suppressing. Suppress only when the type system genuinely can't see what's going on (e.g., `pydantic_settings.BaseSettings` populating fields from env vars).

## Package naming

- Repo / PyPI name: `email-assistant` (with hyphen).
- Importable Python package: `email_agent` (with underscore, NOT `email_assistant`).
- CLI command: `email-agent`.

## Architecture

Ports & adapters (hexagonal), grouped by capability. See `docs/superpowers/specs/2026-05-10-email-assistant-design.md` for the full design.

Each external boundary gets its own package, with `port.py` defining the `Protocol` and adapter modules sitting next to it:

- `src/email_agent/mail/` — `port.py` (`EmailProvider`), `inmemory.py`, later `mailgun.py`.
- `src/email_agent/memory/` — `port.py` (`MemoryPort`), `inmemory.py`, later `cognee.py`.
- `src/email_agent/sandbox/` — `port.py` (`AssistantSandbox`), later `inmemory.py`, `docker.py`.
- `src/email_agent/models/` — pure pydantic data models shared across boundaries.
- `src/email_agent/db/` — SQLAlchemy 2.0 async ORM + Alembic migrations.

Never let the core/domain import from a concrete adapter — depend on the `port` module within each capability package, and let composition wire adapters in at the edge.

## Plans

Implementation plans live in `docs/superpowers/plans/`. The current slice is `2026-05-10-slice-1-core-data-and-ports.md`. Future slices get their own plan files.

## TDD

Red-green-refactor, one failing test at a time. Every task in the plan follows: write failing test → run to confirm failure → minimal implementation → run to confirm pass → commit.
