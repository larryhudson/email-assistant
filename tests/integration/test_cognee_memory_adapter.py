"""Real-cognee integration tests for CogneeMemoryAdapter.

Gated behind `EMAIL_AGENT_E2E=1` and the cognee LLM/embedding keys so CI
doesn't pay for tokens. Run locally with:

    EMAIL_AGENT_E2E=1 \
    COGNEE_LLM_API_KEY=... \
    COGNEE_EMBEDDING_API_KEY=... \
    uv run pytest tests/integration/test_cognee_memory_adapter.py

These tests confirm the *contract* against the real backend: a fact
recorded for one (assistant, thread) is recallable for that pair, and
not visible to another assistant.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from email_agent.memory.cognee import CogneeMemoryAdapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("EMAIL_AGENT_E2E") != "1",
        reason="EMAIL_AGENT_E2E=1 not set",
    ),
    pytest.mark.skipif(
        not os.environ.get("COGNEE_LLM_API_KEY"),
        reason="COGNEE_LLM_API_KEY not set",
    ),
    pytest.mark.skipif(
        not os.environ.get("COGNEE_EMBEDDING_API_KEY"),
        reason="COGNEE_EMBEDDING_API_KEY not set",
    ),
]


@pytest.fixture(autouse=True)
def _configure_cognee_credentials() -> None:
    """Cognee reads LLM_API_KEY / EMBEDDING_API_KEY from env at call time."""
    import cognee

    cognee.config.set_llm_api_key(os.environ["COGNEE_LLM_API_KEY"])
    cognee.config.set_embedding_api_key(os.environ["COGNEE_EMBEDDING_API_KEY"])


async def test_record_then_recall_round_trips(tmp_path: Path) -> None:
    adapter = CogneeMemoryAdapter(data_root=tmp_path)
    await adapter.record_turn("a-1", "t-1", "user", "I love sourdough bread.")

    ctx = await adapter.recall("a-1", "t-1", "What bread does the user like?")

    assert ctx.memories, "expected at least one memory recalled"
    joined = " ".join(m.content.lower() for m in ctx.memories)
    assert "sourdough" in joined


async def test_recall_does_not_leak_across_assistants(tmp_path: Path) -> None:
    adapter = CogneeMemoryAdapter(data_root=tmp_path)
    await adapter.record_turn("a-1", "t-1", "user", "secret-alpha is the password.")
    await adapter.record_turn("a-2", "t-1", "user", "secret-beta is the password.")

    ctx = await adapter.recall("a-2", "t-1", "What is the password?")

    joined = " ".join(m.content.lower() for m in ctx.memories)
    assert "secret-beta" in joined
    assert "secret-alpha" not in joined
