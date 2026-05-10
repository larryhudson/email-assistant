"""Smoke test for the OpenAI-compatible DeepSeek wiring.

Gated behind `EMAIL_AGENT_E2E=1` so CI doesn't pay for tokens.
Run locally with:

    EMAIL_AGENT_E2E=1 \
    DEEPSEEK_API_KEY=sk-... \
    uv run pytest tests/integration/test_deepseek_smoke.py
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
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="DEEPSEEK_API_KEY not set",
    ),
]


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
        model_name="deepseek-chat",
        system_prompt="be terse — one short sentence only.",
    )


async def test_deepseek_returns_non_empty_text() -> None:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com/v1",
    )
    model = OpenAIChatModel("deepseek-chat", provider=provider)

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
