from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.agent import AgentDeps
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.sandbox import ProjectedFile
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

    with agent.override_model(_scope(), TestModel(call_tools=[], custom_output_text="hello back")):
        result = await agent.run(_scope(), prompt="hi", deps=deps)

    assert result.body == "hello back"


def _call_then_echo(tool_name: str, args: dict) -> FunctionModel:
    """FunctionModel that calls one tool, then returns the tool's return as text."""
    state = {"called": False}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["called"]:
            state["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        # Find the most recent tool return and echo it.
        for msg in reversed(messages):
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolReturnPart) and part.tool_name == tool_name:
                    return ModelResponse(parts=[TextPart(content=str(part.content))])
        return ModelResponse(parts=[TextPart(content="(no tool return)")])

    return FunctionModel(fn)


async def test_read_tool_routes_through_sandbox() -> None:
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    await sandbox.ensure_started("a-1")
    await sandbox.project_emails(
        "a-1", [ProjectedFile(path="emails/t-1/thread.md", content=b"# greetings")]
    )

    agent = AssistantAgent()
    deps = AgentDeps(
        assistant_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        sandbox=sandbox,
        memory=memory,
        pending_attachments=[],
    )

    with agent.override_model(
        _scope(),
        _call_then_echo("read", {"path": "emails/t-1/thread.md"}),
    ):
        result = await agent.run(_scope(), prompt="please read", deps=deps)

    assert "greetings" in result.body


async def test_read_tool_returns_error_text_instead_of_raising() -> None:
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

    with agent.override_model(
        _scope(),
        _call_then_echo("read", {"path": "emails/t-1/missing.md"}),
    ):
        result = await agent.run(_scope(), prompt="please read", deps=deps)

    assert "ERROR: read(emails/t-1/missing.md) failed" in result.body
    assert "not found" in result.body


async def test_write_tool_routes_through_sandbox() -> None:
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

    with agent.override_model(
        _scope(),
        _call_then_echo("write", {"path": "notes/draft.md", "content": "hi\n"}),
    ):
        await agent.run(_scope(), prompt="please write", deps=deps)

    from email_agent.models.sandbox import ToolCall

    read_result = await sandbox.run_tool("a-1", "r-1", ToolCall(kind="read", path="notes/draft.md"))
    assert read_result.output == "hi\n"


async def test_edit_tool_routes_through_sandbox() -> None:
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    await sandbox.ensure_started("a-1")
    from email_agent.models.sandbox import ToolCall

    await sandbox.run_tool(
        "a-1", "r-1", ToolCall(kind="write", path="notes/plan.md", content="hello world\n")
    )

    agent = AssistantAgent()
    deps = AgentDeps(
        assistant_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        sandbox=sandbox,
        memory=memory,
        pending_attachments=[],
    )

    with agent.override_model(
        _scope(),
        _call_then_echo("edit", {"path": "notes/plan.md", "old": "world", "new": "planet"}),
    ):
        await agent.run(_scope(), prompt="please edit", deps=deps)

    read_result = await sandbox.run_tool("a-1", "r-1", ToolCall(kind="read", path="notes/plan.md"))
    assert read_result.output == "hello planet\n"


async def test_bash_tool_routes_through_sandbox() -> None:
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

    with agent.override_model(
        _scope(),
        _call_then_echo("bash", {"command": "echo hello"}),
    ):
        result = await agent.run(_scope(), prompt="please bash", deps=deps)

    assert "hello" in result.body


async def test_memory_search_bypasses_sandbox() -> None:
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    await sandbox.ensure_started("a-1")
    await memory.record_turn("a-1", "t-1", "user", "project alpha kicks off Monday")
    await memory.record_turn("a-1", "t-1", "user", "project alpha needs a budget")
    await memory.record_turn("a-1", "t-1", "user", "different topic")

    agent = AssistantAgent()
    deps = AgentDeps(
        assistant_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        sandbox=sandbox,
        memory=memory,
        pending_attachments=[],
    )

    with agent.override_model(
        _scope(),
        _call_then_echo("memory_search", {"query": "project alpha"}),
    ):
        result = await agent.run(_scope(), prompt="search", deps=deps)

    # Both alpha memories appear in the echoed body.
    assert "kicks off Monday" in result.body
    assert "needs a budget" in result.body
    assert "different topic" not in result.body


async def test_attach_file_appends_pending_attachment() -> None:
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    await sandbox.ensure_started("a-1")
    from email_agent.models.sandbox import ToolCall as _ToolCall

    await sandbox.run_tool(
        "a-1",
        "r-1",
        _ToolCall(kind="write", path="report.pdf", content="%PDF-1.7"),
    )

    agent = AssistantAgent()
    deps = AgentDeps(
        assistant_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        sandbox=sandbox,
        memory=memory,
        pending_attachments=[],
    )

    with agent.override_model(
        _scope(),
        _call_then_echo("attach_file", {"path": "report.pdf", "filename": "renamed.pdf"}),
    ):
        await agent.run(_scope(), prompt="please attach", deps=deps)

    from email_agent.models.sandbox import PendingAttachment

    assert deps.pending_attachments == [
        PendingAttachment(sandbox_path="report.pdf", filename="renamed.pdf")
    ]


async def test_attach_file_defaults_filename_to_basename() -> None:
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    await sandbox.ensure_started("a-1")
    from email_agent.models.sandbox import ToolCall as _ToolCall

    await sandbox.run_tool(
        "a-1",
        "r-1",
        _ToolCall(kind="write", path="docs/report.pdf", content="%PDF-1.7"),
    )

    agent = AssistantAgent()
    deps = AgentDeps(
        assistant_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        sandbox=sandbox,
        memory=memory,
        pending_attachments=[],
    )

    with agent.override_model(
        _scope(),
        _call_then_echo("attach_file", {"path": "docs/report.pdf"}),
    ):
        await agent.run(_scope(), prompt="please attach", deps=deps)

    from email_agent.models.sandbox import PendingAttachment

    assert deps.pending_attachments == [
        PendingAttachment(sandbox_path="docs/report.pdf", filename="report.pdf")
    ]


async def test_attach_file_returns_error_text_instead_of_raising() -> None:
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

    with agent.override_model(
        _scope(),
        _call_then_echo("attach_file", {"path": "missing.pdf"}),
    ):
        result = await agent.run(_scope(), prompt="please attach", deps=deps)

    assert "ERROR: attach_file(missing.pdf) failed" in result.body
    assert "not found" in result.body
    assert deps.pending_attachments == []
