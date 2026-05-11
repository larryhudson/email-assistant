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
from email_agent.agent.toolset import AgentToolset
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.agent import AgentDeps
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.sandbox import PendingAttachment
from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace


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


def _deps(
    *,
    env: InMemoryEnvironment | None = None,
    memory: InMemoryMemoryAdapter | None = None,
    pending: list[PendingAttachment] | None = None,
) -> AgentDeps:
    actual_env = env or InMemoryEnvironment()
    actual_memory = memory or InMemoryMemoryAdapter()
    actual_pending = pending if pending is not None else []
    return AgentDeps(
        assistant_id="a-1",
        run_id="r-1",
        thread_id="t-1",
        toolset=AgentToolset(
            assistant_id="a-1",
            run_id="r-1",
            env=actual_env,
            workspace=AssistantWorkspace(actual_env),
            memory=actual_memory,
            pending_attachments=actual_pending,
        ),
        pending_attachments=actual_pending,
    )


async def test_assistant_agent_returns_text_output() -> None:
    agent = AssistantAgent()
    deps = _deps()

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


async def test_read_tool_routes_through_toolset() -> None:
    env = InMemoryEnvironment()
    await AssistantWorkspace(env).project_emails([])
    await env.write_text("emails/t-1/thread.md", "# greetings")

    agent = AssistantAgent()
    deps = _deps(env=env)

    with agent.override_model(
        _scope(),
        _call_then_echo("read", {"path": "emails/t-1/thread.md"}),
    ):
        result = await agent.run(_scope(), prompt="please read", deps=deps)

    assert "greetings" in result.body


async def test_read_tool_returns_error_text_instead_of_raising() -> None:
    agent = AssistantAgent()
    deps = _deps()

    with agent.override_model(
        _scope(),
        _call_then_echo("read", {"path": "emails/t-1/missing.md"}),
    ):
        result = await agent.run(_scope(), prompt="please read", deps=deps)

    assert "ERROR: read(emails/t-1/missing.md) failed" in result.body
    assert "not found" in result.body


async def test_write_tool_routes_through_toolset() -> None:
    env = InMemoryEnvironment()

    agent = AssistantAgent()
    deps = _deps(env=env)

    with agent.override_model(
        _scope(),
        _call_then_echo("write", {"path": "notes/draft.md", "content": "hi\n"}),
    ):
        await agent.run(_scope(), prompt="please write", deps=deps)

    assert await env.read_text("notes/draft.md") == "hi\n"


async def test_edit_tool_routes_through_toolset() -> None:
    env = InMemoryEnvironment()
    await env.write_text("notes/plan.md", "hello world\n")

    agent = AssistantAgent()
    deps = _deps(env=env)

    with agent.override_model(
        _scope(),
        _call_then_echo("edit", {"path": "notes/plan.md", "old": "world", "new": "planet"}),
    ):
        await agent.run(_scope(), prompt="please edit", deps=deps)

    assert await env.read_text("notes/plan.md") == "hello planet\n"


async def test_bash_tool_routes_through_toolset() -> None:
    agent = AssistantAgent()
    deps = _deps()

    with agent.override_model(
        _scope(),
        _call_then_echo("bash", {"command": "echo hello"}),
    ):
        result = await agent.run(_scope(), prompt="please bash", deps=deps)

    assert "hello" in result.body


async def test_memory_search_bypasses_sandbox() -> None:
    memory = InMemoryMemoryAdapter()
    await memory.record_turn("a-1", "t-1", "user", "project alpha kicks off Monday")
    await memory.record_turn("a-1", "t-1", "user", "project alpha needs a budget")
    await memory.record_turn("a-1", "t-1", "user", "different topic")

    agent = AssistantAgent()
    deps = _deps(memory=memory)

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
    env = InMemoryEnvironment()
    await env.write_text("report.pdf", "%PDF-1.7")
    pending: list[PendingAttachment] = []

    agent = AssistantAgent()
    deps = _deps(env=env, pending=pending)

    with agent.override_model(
        _scope(),
        _call_then_echo("attach_file", {"path": "report.pdf", "filename": "renamed.pdf"}),
    ):
        await agent.run(_scope(), prompt="please attach", deps=deps)

    assert pending == [PendingAttachment(sandbox_path="report.pdf", filename="renamed.pdf")]


async def test_attach_file_defaults_filename_to_basename() -> None:
    env = InMemoryEnvironment()
    await env.write_text("docs/report.pdf", "%PDF-1.7")
    pending: list[PendingAttachment] = []

    agent = AssistantAgent()
    deps = _deps(env=env, pending=pending)

    with agent.override_model(
        _scope(),
        _call_then_echo("attach_file", {"path": "docs/report.pdf"}),
    ):
        await agent.run(_scope(), prompt="please attach", deps=deps)

    assert pending == [PendingAttachment(sandbox_path="docs/report.pdf", filename="report.pdf")]


async def test_attach_file_returns_error_text_instead_of_raising() -> None:
    agent = AssistantAgent()
    deps = _deps()

    with agent.override_model(
        _scope(),
        _call_then_echo("attach_file", {"path": "missing.pdf"}),
    ):
        result = await agent.run(_scope(), prompt="please attach", deps=deps)

    assert "ERROR: attach_file(missing.pdf) failed" in result.body
    assert "not found" in result.body
    assert deps.pending_attachments == []
