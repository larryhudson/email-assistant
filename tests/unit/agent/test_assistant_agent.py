import pytest
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
from email_agent.models.agent import AgentDeps, MeteredUsage
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.sandbox import PendingAttachment
from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace
from email_agent.search.inmemory import InMemorySearchAdapter
from email_agent.search.port import SearchResult


def _scope() -> AssistantScope:
    return AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        owner_email="owner@example.com",
        end_user_id="u-1",
        end_user_email="mum@example.com",
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
    skills_block: str = "",
    context_block: str = "",
    scheduled_tasks: object | None = None,
    search: InMemorySearchAdapter | None = None,
    metered: list[MeteredUsage] | None = None,
) -> AgentDeps:
    actual_env = env or InMemoryEnvironment()
    actual_memory = memory or InMemoryMemoryAdapter()
    actual_pending = pending if pending is not None else []
    actual_metered = metered if metered is not None else []
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
            metered_usage=actual_metered,
            search=search,
            scheduled_tasks=scheduled_tasks,  # ty: ignore[invalid-argument-type]
        ),
        pending_attachments=actual_pending,
        metered_usage=actual_metered,
        skills_block=skills_block,
        context_block=context_block,
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


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct read tool calls.", strict=True
)
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


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct read tool calls.", strict=True
)
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


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct write tool calls.", strict=True
)
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


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct edit tool calls.", strict=True
)
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


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct bash tool calls.", strict=True
)
async def test_bash_tool_routes_through_toolset() -> None:
    agent = AssistantAgent()
    deps = _deps()

    with agent.override_model(
        _scope(),
        _call_then_echo("bash", {"command": "echo hello"}),
    ):
        result = await agent.run(_scope(), prompt="please bash", deps=deps)

    assert "hello" in result.body


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct memory_search calls.", strict=True
)
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


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct web_search calls.", strict=True
)
async def test_web_search_tool_routes_through_host_search_adapter() -> None:
    from decimal import Decimal

    search = InMemorySearchAdapter(
        results=[
            SearchResult(
                title="Current result",
                url="https://example.com/current",
                snippet="current public fact",
            )
        ],
        cost_usd=Decimal("0.0050"),
    )
    metered: list[MeteredUsage] = []
    agent = AssistantAgent(has_web_search=True)
    deps = _deps(search=search, metered=metered)

    with agent.override_model(
        _scope(),
        _call_then_echo("web_search", {"query": "current fact", "max_results": 2}),
    ):
        result = await agent.run(_scope(), prompt="search", deps=deps)

    assert search.calls == [("current fact", 2)]
    assert "UNTRUSTED EXTERNAL WEB SEARCH RESULTS" in result.body
    assert "current public fact" in result.body
    assert result.metered_usage == metered
    assert result.steps[0].kind == "tool:web_search"
    assert result.steps[0].cost_usd == Decimal("0.0050")


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct attach_file calls.", strict=True
)
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


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct attach_file calls.", strict=True
)
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


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct attach_file calls.", strict=True
)
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


def _capture_instructions() -> tuple[FunctionModel, dict[str, str | None]]:
    captured: dict[str, str | None] = {}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn), captured


async def test_context_block_is_injected_into_instructions() -> None:
    agent = AssistantAgent()
    deps = _deps(context_block="# CONTEXT.md\n\nuser is in AEST")

    model, captured = _capture_instructions()
    with agent.override_model(_scope(), model):
        await agent.run(_scope(), prompt="hi", deps=deps)

    assert captured["instructions"] is not None
    assert "user is in AEST" in captured["instructions"]


async def test_skills_block_is_injected_into_instructions() -> None:
    agent = AssistantAgent()
    deps = _deps(skills_block="# Available skills\n\n## triage\nbody")

    model, captured = _capture_instructions()
    with agent.override_model(_scope(), model):
        await agent.run(_scope(), prompt="hi", deps=deps)

    assert captured["instructions"] is not None
    assert "triage" in captured["instructions"]


async def test_workspace_guidance_is_part_of_base_instructions() -> None:
    """Even with empty skills/context, the agent is told about CONTEXT.md & skills."""
    agent = AssistantAgent()
    deps = _deps()

    model, captured = _capture_instructions()
    with agent.override_model(_scope(), model):
        await agent.run(_scope(), prompt="hi", deps=deps)

    assert captured["instructions"] is not None
    assert "CONTEXT.md" in captured["instructions"]
    assert "skills" in captured["instructions"].lower()


async def test_scope_system_prompt_still_present() -> None:
    agent = AssistantAgent()
    deps = _deps()

    model, captured = _capture_instructions()
    with agent.override_model(_scope(), model):
        await agent.run(_scope(), prompt="hi", deps=deps)

    assert captured["instructions"] is not None
    assert "be kind" in captured["instructions"]


async def test_code_mode_run_code_routes_workspace_tools_through_toolset() -> None:
    env = InMemoryEnvironment()
    agent = AssistantAgent()
    deps = _deps(env=env)

    code = (
        "write_result = await write(path='notes/code-mode.md', content='hello from code mode')\n"
        "read_result = await read(path='notes/code-mode.md')\n"
        "bash_result = await bash(command='printf bash-ok')\n"
        "{'write': write_result, 'read': read_result, 'bash': bash_result}"
    )

    with agent.override_model(_scope(), _call_then_echo("run_code", {"code": code})):
        result = await agent.run(_scope(), prompt="use code mode", deps=deps)

    assert await env.read_text("notes/code-mode.md") == "hello from code mode"
    assert "wrote notes/code-mode.md" in result.body
    assert "hello from code mode" in result.body
    assert "bash-ok" in result.body
    assert result.steps[0].kind == "tool:run_code"


class _FakeScheduledTasks:
    """Tiny in-memory stand-in for ScheduledTaskService used by agent-tool tests."""

    def __init__(self) -> None:
        from email_agent.models.scheduled import ScheduledTask

        self.created: list[dict] = []
        self.deleted: list[str] = []
        self._items: list[ScheduledTask] = []

    async def create_once(self, *, assistant_id, run_at, name, body, created_by_run_id=None):
        from email_agent.models.scheduled import (
            ScheduledTask,
            ScheduledTaskKind,
            ScheduledTaskStatus,
        )

        self.created.append(
            {"assistant_id": assistant_id, "run_at": run_at, "name": name, "body": body}
        )
        task = ScheduledTask(
            id="st-1",
            assistant_id=assistant_id,
            kind=ScheduledTaskKind.ONCE,
            run_at=run_at,
            cron_expr=None,
            next_run_at=run_at,
            last_run_at=None,
            status=ScheduledTaskStatus.ACTIVE,
            name=name,
            body=body,
            created_by_run_id=created_by_run_id,
            created_at=run_at,
            updated_at=run_at,
        )
        self._items.append(task)
        return task

    async def create_cron(
        self, *, assistant_id, cron_expr, name, body, created_by_run_id=None
    ):  # pragma: no cover - not exercised here
        raise NotImplementedError

    async def list_for_assistant(self, assistant_id):
        return [t for t in self._items if t.assistant_id == assistant_id]

    async def delete(self, *, assistant_id, task_id):
        before = len(self._items)
        self._items = [
            t for t in self._items if not (t.id == task_id and t.assistant_id == assistant_id)
        ]
        if len(self._items) < before:
            self.deleted.append(task_id)
            return True
        return False


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct create_scheduled_task calls.",
    strict=True,
)
async def test_create_scheduled_task_tool_routes_through_toolset() -> None:
    fake = _FakeScheduledTasks()
    agent = AssistantAgent()
    deps = _deps(scheduled_tasks=fake)

    with agent.override_model(
        _scope(),
        _call_then_echo(
            "create_scheduled_task",
            {
                "kind": "once",
                "when": "2026-05-12T09:00:00+00:00",
                "name": "ping",
                "body": "ping body",
            },
        ),
    ):
        result = await agent.run(_scope(), prompt="schedule it", deps=deps)

    assert len(fake.created) == 1
    assert fake.created[0]["name"] == "ping"
    assert "created scheduled_task" in result.body


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct list_scheduled_tasks calls.",
    strict=True,
)
async def test_list_scheduled_tasks_tool_routes_through_toolset() -> None:
    from datetime import UTC, datetime

    fake = _FakeScheduledTasks()
    await fake.create_once(
        assistant_id="a-1",
        run_at=datetime(2026, 5, 12, tzinfo=UTC),
        name="weekly-review",
        body="review",
    )
    agent = AssistantAgent()
    deps = _deps(scheduled_tasks=fake)

    with agent.override_model(_scope(), _call_then_echo("list_scheduled_tasks", {})):
        result = await agent.run(_scope(), prompt="list please", deps=deps)

    assert "weekly-review" in result.body


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct delete_scheduled_task calls.",
    strict=True,
)
async def test_delete_scheduled_task_tool_routes_through_toolset() -> None:
    from datetime import UTC, datetime

    fake = _FakeScheduledTasks()
    await fake.create_once(
        assistant_id="a-1",
        run_at=datetime(2026, 5, 12, tzinfo=UTC),
        name="gone",
        body="x",
    )
    agent = AssistantAgent()
    deps = _deps(scheduled_tasks=fake)

    with agent.override_model(
        _scope(),
        _call_then_echo("delete_scheduled_task", {"task_id": "st-1"}),
    ):
        result = await agent.run(_scope(), prompt="delete it", deps=deps)

    assert fake.deleted == ["st-1"]
    assert "deleted scheduled_task st-1" in result.body


async def test_memory_search_tool_not_registered_when_memory_disabled() -> None:
    agent = AssistantAgent(has_memory=False)
    # Force the lazy-build path so we can introspect the underlying pydantic-ai
    # Agent's registered tools.
    built = agent._agent_for(_scope())
    tool_names = set(built._function_toolset.tools.keys())
    assert "memory_search" not in tool_names
    # Other tools are still there.
    assert "read" in tool_names
    assert "write" in tool_names


async def test_memory_search_tool_registered_by_default() -> None:
    agent = AssistantAgent()
    built = agent._agent_for(_scope())
    tool_names = set(built._function_toolset.tools.keys())
    assert "memory_search" in tool_names


async def test_web_search_tool_registered_only_when_enabled() -> None:
    assert "web_search" not in AssistantAgent()._agent_for(_scope())._function_toolset.tools
    assert (
        "web_search"
        in AssistantAgent(has_web_search=True)._agent_for(_scope())._function_toolset.tools
    )


async def test_run_wraps_underlying_exception_with_partial_usage_and_steps() -> None:
    """When the agent crashes after some tool calls, AssistantAgent.run must
    raise AgentRunError carrying the partial usage + step trace so the
    recorder can persist them (otherwise the budget cap drifts silently
    and the admin trace is empty for failed runs).
    """
    import pytest

    from email_agent.models.agent import AgentRunError

    call_count = {"n": 0}

    async def crash_after_one_tool(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="read", args={"path": "x"})])
        raise RuntimeError("model boom")

    agent = AssistantAgent()
    deps = _deps()

    with (
        pytest.raises(AgentRunError) as excinfo,
        agent.override_model(_scope(), FunctionModel(crash_after_one_tool)),
    ):
        await agent.run(_scope(), prompt="trigger", deps=deps)

    failed = excinfo.value
    assert isinstance(failed.original, RuntimeError)
    assert "model boom" in str(failed.original)
    # Captured usage from the one successful model response before the crash.
    assert failed.usage.input_tokens > 0
    assert failed.usage.output_tokens > 0
    # Step trace contains the read tool call that succeeded.
    kinds = [s.kind for s in failed.steps]
    assert "tool:read" in kinds
