"""Smoke test for the Fireworks AI provider wiring.

Gated behind `EMAIL_AGENT_E2E=1` so CI doesn't pay for tokens.
Run locally with:

    EMAIL_AGENT_E2E=1 \
    FIREWORKS_API_KEY=... \
    uv run pytest tests/integration/test_fireworks_smoke.py
"""

import os

import pytest

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.agent import AgentDeps
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.sandbox.inmemory import InMemorySandbox

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("EMAIL_AGENT_E2E") != "1",
        reason="EMAIL_AGENT_E2E=1 not set",
    ),
    pytest.mark.skipif(
        not os.environ.get("FIREWORKS_API_KEY"),
        reason="FIREWORKS_API_KEY not set",
    ),
]


_FIREWORKS_MODEL_ID = os.environ.get("FIREWORKS_MODEL_ID", "accounts/fireworks/models/minimax-m2p7")


def _scope() -> AssistantScope:
    return AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        end_user_id="u-1",
        inbound_address="mum@assistants.example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="mum",
        tool_allowlist=("read",),
        budget_id="b-1",
        model_name=_FIREWORKS_MODEL_ID,
        system_prompt="be terse — one short sentence only.",
    )


async def test_fireworks_returns_non_empty_text() -> None:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.fireworks import FireworksProvider

    provider = FireworksProvider(api_key=os.environ["FIREWORKS_API_KEY"])
    model = OpenAIChatModel(_FIREWORKS_MODEL_ID, provider=provider)

    sandbox = InMemorySandbox()
    await sandbox.ensure_started("a-1")
    deps = AgentDeps(
        assistant_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        sandbox=sandbox,
        memory=InMemoryMemoryAdapter(),
        pending_attachments=[],
    )

    agent = AssistantAgent()
    with agent.override_model(_scope(), model):
        result = await agent.run(_scope(), prompt="Say hi.", deps=deps)

    assert result.body
    assert result.usage.input_tokens > 0
