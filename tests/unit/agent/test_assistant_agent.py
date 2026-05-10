from pydantic_ai.models.test import TestModel

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.agent import AgentDeps
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.sandbox.inmemory import InMemorySandbox


def _scope() -> AssistantScope:
    return AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        end_user_id="u-1",
        inbound_address="mum@assistants.example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="mum",
        tool_allowlist=("read", "write", "edit", "bash", "memory_search", "attach_file"),
        budget_id="b-1",
        model_name="test-model",
        system_prompt="be kind",
    )


async def test_assistant_agent_returns_text_output() -> None:
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    await sandbox.ensure_started("a-1")

    agent = AssistantAgent()
    deps = AgentDeps(
        assistant_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        sandbox=sandbox,
        memory=memory,
        pending_attachments=[],
    )

    with agent.override_model(_scope(), TestModel(custom_output_text="hello back")):
        result = await agent.run(_scope(), prompt="hi", deps=deps)

    assert result.body == "hello back"
