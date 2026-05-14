from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EndUser,
    Owner,
    UsageLedger,
)
from email_agent.domain.budget_governor import BudgetGovernor
from email_agent.domain.reply_envelope import ReplyEnvelopeBuilder
from email_agent.domain.run_recorder import RunRecorder
from email_agent.domain.workspace_projector import EmailWorkspaceProjector
from email_agent.mail.inmemory import InMemoryEmailProvider
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import NormalizedInboundEmail
from email_agent.runtime.assistant_runtime import AssistantRuntime, Completed
from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace
from email_agent.sandbox.workspace_provider import StaticWorkspaceProvider, WorkspaceProvider
from email_agent.search.inmemory import InMemorySearchAdapter
from email_agent.search.port import SearchResult


async def _seed_assistant(session: AsyncSession) -> None:
    session.add(Owner(id="o-1", name="Larry", email="owner@example.com"))
    session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
    session.add(
        Budget(
            id="b-1",
            assistant_id="a-1",
            monthly_limit_usd=Decimal("10.00"),
            period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    session.add(
        Assistant(
            id="a-1",
            end_user_id="u-1",
            inbound_address="mum@assistants.example.com",
            status="active",
            allowed_senders=["mum@example.com"],
            model="test-model",
            system_prompt="be kind",
        )
    )
    session.add(
        AssistantScopeRow(
            assistant_id="a-1",
            memory_namespace="mum",
            tool_allowlist=["read"],
            budget_id="b-1",
        )
    )
    await session.commit()


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


def _inbound() -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id="prov-1",
        message_id_header="<m1@x>",
        from_email="mum@example.com",
        to_emails=["mum@assistants.example.com"],
        subject="hello?",
        body_text="please reply",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


def _read_then_reply() -> FunctionModel:
    """Script: model parses the path out of the prompt, calls read, then replies."""
    import re

    state = {"called": False}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["called"]:
            state["called"] = True
            # The runtime puts the projected message path in the prompt as
            # 'emails/.../NNNN-...md'. Pull it out so the test stays decoupled
            # from the auto-generated thread id.
            text = " ".join(
                part.content
                for msg in messages
                for part in getattr(msg, "parts", [])
                if hasattr(part, "content") and isinstance(part.content, str)
            )
            match = re.search(r"emails/[^\s'\"]+\.md", text)
            assert match is not None, f"no projected path in prompt: {text!r}"
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read", args={"path": match.group(0)})]
            )
        for msg in reversed(messages):
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolReturnPart):
                    return ModelResponse(parts=[TextPart(content="Re: thanks!")])
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn)


def _build_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tmp_path: Path,
    email_provider: InMemoryEmailProvider,
    workspace_provider: WorkspaceProvider,
    memory: InMemoryMemoryAdapter,
    agent: AssistantAgent,
    search: InMemorySearchAdapter | None = None,
) -> AssistantRuntime:
    return AssistantRuntime(
        session_factory,
        attachments_root=tmp_path / "attachments",
        email_provider=email_provider,
        workspace_provider=workspace_provider,
        memory=memory,
        agent=agent,
        projector=EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs"),
        recorder=RunRecorder(session_factory),
        budget_governor=BudgetGovernor(session_factory),
        envelope_builder=ReplyEnvelopeBuilder(),
        message_id_factory=lambda: "<run-abc@assistants.example.com>",
        provider_message_id_factory=lambda: "prov-out-1",
        search=search,
    )


async def _run_id_for(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    async with session_factory() as session:
        row = (await session.execute(select(AgentRun))).scalar_one()
        return row.id


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct read tool calls.", strict=True
)
async def test_execute_run_sends_reply_and_records_completion(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,
        agent=agent,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with agent.override_model(_scope(), _read_then_reply()):
        outcome = await runtime.execute_run(run_id)

    assert isinstance(outcome, Completed)

    # Reply was sent through the email provider.
    assert len(email_provider.sent) == 1
    sent = email_provider.sent[0]
    assert sent.body_text.startswith("Re: thanks!")
    assert sent.in_reply_to_header == "<m1@x>"
    assert sent.references_headers == ["<m1@x>"]
    assert sent.subject == "Re: hello?"

    # AgentRun marked completed with reply linkage.
    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "completed"
        assert run.reply_message_id is not None

        outbound_msgs = (
            (
                await session.execute(
                    select(EmailMessage).where(EmailMessage.direction == "outbound")
                )
            )
            .scalars()
            .all()
        )
        assert len(outbound_msgs) == 1

        usage_rows = (await session.execute(select(UsageLedger))).scalars().all()
        assert len(usage_rows) == 1


async def test_execute_run_sends_reply_with_code_mode_run_code(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    import re

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,
        agent=agent,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)
    state = {"called": False}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["called"]:
            state["called"] = True
            text = " ".join(
                part.content
                for msg in messages
                for part in getattr(msg, "parts", [])
                if hasattr(part, "content") and isinstance(part.content, str)
            )
            match = re.search(r"emails/[^\s'\"]+\.md", text)
            assert match is not None, f"no projected path in prompt: {text!r}"
            code = f"message = await read(path={match.group(0)!r})\nmessage"
            return ModelResponse(parts=[ToolCallPart(tool_name="run_code", args={"code": code})])
        for msg in reversed(messages):
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolReturnPart) and part.tool_name == "run_code":
                    assert "please reply" in str(part.content)
                    return ModelResponse(parts=[TextPart(content="Re: thanks!")])
        return ModelResponse(parts=[TextPart(content="ok")])

    with agent.override_model(_scope(), FunctionModel(fn)):
        outcome = await runtime.execute_run(run_id)

    assert isinstance(outcome, Completed)
    assert len(email_provider.sent) == 1
    assert email_provider.sent[0].body_text.startswith("Re: thanks!")

    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "completed"
        assert run.reply_message_id is not None


def _search_then_reply() -> FunctionModel:
    state = {"called": False}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["called"]:
            state["called"] = True
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="web_search",
                        args={"query": "current public fact", "max_results": 1},
                    )
                ]
            )
        for msg in reversed(messages):
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolReturnPart):
                    assert "UNTRUSTED EXTERNAL WEB SEARCH RESULTS" in str(part.content)
                    return ModelResponse(parts=[TextPart(content="Found it.")])
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn)


@pytest.mark.xfail(
    reason="Code mode exposes run_code instead of direct web_search tool calls.",
    strict=True,
)
async def test_execute_run_records_web_search_tool_cost(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    from email_agent.db.models import RunStep

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

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
    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent(has_web_search=True)
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,
        agent=agent,
        search=search,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with agent.override_model(_scope(), _search_then_reply()):
        outcome = await runtime.execute_run(run_id)

    assert isinstance(outcome, Completed)
    assert search.calls == [("current public fact", 1)]

    async with sqlite_session_factory() as session:
        usage_rows = (await session.execute(select(UsageLedger))).scalars().all()
        assert {u.provider for u in usage_rows} == {"openai-compat", "brave"}
        brave = next(u for u in usage_rows if u.provider == "brave")
        assert brave.cost_usd == Decimal("0.0050")
        assert brave.input_tokens == 0
        assert brave.output_tokens == 0

        steps = (await session.execute(select(RunStep))).scalars().all()
        search_step = next(s for s in steps if s.kind == "tool:web_search")
        assert search_step.cost_usd == Decimal("0.0050")


async def test_owner_inbound_cc_routes_reply_to_owner_with_end_user_cc(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """When the owner emails the assistant, the reply must address the owner
    and cc the end-user so the end-user stays in the loop on admin threads."""
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        # Allow the owner to email this assistant.
        from email_agent.db.models import Assistant as AssistantRow

        a = await session.get(AssistantRow, "a-1")
        assert a is not None
        a.allowed_senders = ["mum@example.com", "owner@example.com"]
        await session.commit()

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=InMemoryMemoryAdapter(),
        agent=agent,
    )

    # Inbound from the owner, not the end-user.
    owner_inbound = NormalizedInboundEmail(
        provider_message_id="prov-owner-1",
        message_id_header="<m-owner-1@x>",
        from_email="owner@example.com",
        to_emails=["mum@assistants.example.com"],
        subject="config tweak",
        body_text="bump the daily check-in to 8am please",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )
    await runtime.accept_inbound(owner_inbound)
    run_id = await _run_id_for(sqlite_session_factory)

    with agent.override_model(_scope(), _read_then_reply()):
        outcome = await runtime.execute_run(run_id)

    assert isinstance(outcome, Completed)

    sent = email_provider.sent[0]
    assert sent.to_emails == ["owner@example.com"]
    assert sent.cc_emails == ["mum@example.com"]


async def test_end_user_inbound_does_not_cc_owner(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """When the end-user emails the assistant, the reply has no cc."""
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=InMemoryMemoryAdapter(),
        agent=agent,
    )

    await runtime.accept_inbound(_inbound())  # from mum@example.com
    run_id = await _run_id_for(sqlite_session_factory)

    with agent.override_model(_scope(), _read_then_reply()):
        await runtime.execute_run(run_id)

    assert email_provider.sent[0].cc_emails == []


async def test_execute_run_injects_recalled_memory_into_prompt(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """The runtime must call MemoryPort.recall(assistant_id, thread_id, body) once
    before the agent runs and inject the returned memories into the prompt, so the
    model sees prior context without needing to call memory_search itself."""
    from datetime import UTC
    from datetime import datetime as _dt

    from email_agent.models.memory import Memory, MemoryContext

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    class RecordingMemory:
        def __init__(self) -> None:
            self.recall_calls: list[tuple[str, str, str]] = []

        async def recall(self, *, assistant_id: str, thread_id: str, query: str) -> MemoryContext:
            self.recall_calls.append((assistant_id, thread_id, query))
            return MemoryContext(
                memories=[
                    Memory(
                        id="seed-1",
                        content="REMEMBERED-FACT-XYZ: prior context for the agent.",
                    )
                ],
                retrieved_at=_dt.now(UTC),
            )

        async def record_turn(self, *args, **kwargs) -> None:
            pass

        async def search(self, assistant_id: str, query: str) -> list[Memory]:
            return []

        async def delete_assistant(self, assistant_id: str) -> None:
            pass

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    memory = RecordingMemory()
    agent = AssistantAgent()

    captured_prompts: list[str] = []

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        text = " ".join(
            part.content
            for msg in messages
            for part in getattr(msg, "parts", [])
            if hasattr(part, "content") and isinstance(part.content, str)
        )
        captured_prompts.append(text)
        return ModelResponse(parts=[TextPart(content="ok")])

    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,  # ty: ignore[invalid-argument-type]
        agent=agent,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with agent.override_model(_scope(), FunctionModel(fn)):
        outcome = await runtime.execute_run(run_id)

    assert isinstance(outcome, Completed)
    assert captured_prompts, "model was never called"
    assert any("REMEMBERED-FACT-XYZ" in p for p in captured_prompts), (
        f"recalled memory not in prompt; saw: {captured_prompts!r}"
    )
    # recall was called with the inbound body as the query.
    assert len(memory.recall_calls) == 1
    a_id, _t_id, query = memory.recall_calls[0]
    assert a_id == "a-1"
    assert "please reply" in query

    # Recalled memories were persisted to RunMemoryRecall so the admin
    # UI can show the agent exactly what context it had.
    from email_agent.db.models import RunMemoryRecall

    async with sqlite_session_factory() as session:
        rows = (
            (await session.execute(select(RunMemoryRecall).where(RunMemoryRecall.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].content == "REMEMBERED-FACT-XYZ: prior context for the agent."


async def test_execute_run_sends_template_when_budget_exceeded(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    from email_agent.db.models import UsageLedger as _UsageLedger

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        # Pre-stage a usage_ledger row that takes the assistant at-cap.
        session.add(
            _UsageLedger(
                id="u-old",
                assistant_id="a-1",
                run_id="seed",
                provider="seed",
                model="seed",
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("10.00"),
                budget_period="2026-05",
                created_at=datetime(2026, 5, 5, tzinfo=UTC),
            )
        )
        # The seed run's UsageLedger references a non-existent run_id; SQLite
        # doesn't enforce FK by default in our test setup.
        await session.commit()

    email_provider = InMemoryEmailProvider()
    env = InMemoryEnvironment()
    workspace = AssistantWorkspace(env)
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,
        agent=agent,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    outcome = await runtime.execute_run(run_id)

    from email_agent.runtime.assistant_runtime import BudgetLimited

    assert isinstance(outcome, BudgetLimited)

    # Template body, not a model output — should mention "monthly budget".
    assert len(email_provider.sent) == 1
    body = email_provider.sent[0].body_text
    assert "monthly budget" in body.lower()

    # Workspace was never touched.
    assert not await env.exists("/workspace/emails")

    # Run marked budget_limited, not completed.
    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
        assert run.status == "budget_limited"


def _failing_model() -> FunctionModel:
    """Model that raises on first call so the agent surfaces the failure."""

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ValueError("model exploded")

    return FunctionModel(fn)


async def test_execute_run_records_failed_run_and_reraises(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    import pytest

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,
        agent=agent,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with (
        agent.override_model(_scope(), _failing_model()),
        pytest.raises(ValueError, match="exploded"),
    ):
        await runtime.execute_run(run_id)

    # Both notifications fire: an apology to the end-user (threaded reply) and
    # an alert to the owner (separate envelope).
    assert len(email_provider.sent) == 2
    apology = next(s for s in email_provider.sent if s.in_reply_to_header == "<m1@x>")
    assert apology.to_emails == ["mum@example.com"]
    assert "exploded" not in apology.body_text

    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "failed"
        assert run.error is not None
        assert "exploded" in run.error


async def test_execute_run_notifies_end_user_and_owner_on_unhandled_exception(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    import pytest

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        owner = await session.get(Owner, "o-1")
        assert owner is not None
        owner.email = "admin@example.com"
        await session.commit()

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path / "attachments",
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,
        agent=agent,
        projector=EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs"),
        recorder=RunRecorder(sqlite_session_factory),
        budget_governor=BudgetGovernor(sqlite_session_factory),
        envelope_builder=ReplyEnvelopeBuilder(),
        message_id_factory=lambda: "<run-abc@x>",
        provider_message_id_factory=lambda: "prov-out-1",
        admin_base_url="https://admin.example.com",
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with (
        agent.override_model(_scope(), _failing_model()),
        pytest.raises(ValueError, match="exploded"),
    ):
        await runtime.execute_run(run_id)

    assert len(email_provider.sent) == 2
    end_user, owner_msg = email_provider.sent

    # End-user envelope: threaded reply, no exception details, no footer.
    assert end_user.to_emails == ["mum@example.com"]
    assert end_user.in_reply_to_header == "<m1@x>"
    assert end_user.references_headers == ["<m1@x>"]
    assert end_user.subject == "Re: hello?"
    assert run_id in end_user.body_text
    assert "exploded" not in end_user.body_text
    assert "ValueError" not in end_user.body_text
    assert "email-agent run footer" not in end_user.body_text

    # Owner envelope: fresh, unthreaded, technical, no footer.
    assert owner_msg.to_emails == ["admin@example.com"]
    assert owner_msg.in_reply_to_header is None
    assert owner_msg.references_headers == []
    assert owner_msg.subject == f"[email-agent] run {run_id} failed"
    assert "ValueError" in owner_msg.body_text
    assert "exploded" in owner_msg.body_text
    assert run_id in owner_msg.body_text
    assert f"https://admin.example.com/admin/runs/{run_id}" in owner_msg.body_text
    assert "email-agent run footer" not in owner_msg.body_text

    # Run remains durably Failed even though notifications fired.
    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "failed"


async def test_owner_envelope_omits_admin_url_when_unset(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    import pytest

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        owner = await session.get(Owner, "o-1")
        assert owner is not None
        owner.email = "admin@example.com"
        await session.commit()

    email_provider = InMemoryEmailProvider()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(AssistantWorkspace(InMemoryEnvironment())),
        memory=InMemoryMemoryAdapter(),
        agent=AssistantAgent(),
    )
    # _build_runtime doesn't set admin_base_url; verify URL is absent.

    agent = runtime._agent
    assert agent is not None
    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with (
        agent.override_model(_scope(), _failing_model()),
        pytest.raises(ValueError, match="exploded"),
    ):
        await runtime.execute_run(run_id)

    owner_msg = email_provider.sent[1]
    assert "Admin:" not in owner_msg.body_text
    assert "/admin/runs/" not in owner_msg.body_text


async def test_notification_send_failures_are_swallowed(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """If sending the end-user note raises, the owner note is still
    attempted; if both raise, neither escapes — the original failure is
    what propagates."""
    import pytest

    from email_agent.models.email import NormalizedOutboundEmail, SentEmail

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        owner = await session.get(Owner, "o-1")
        assert owner is not None
        owner.email = "admin@example.com"
        await session.commit()

    class ExplodingProvider:
        def __init__(self) -> None:
            self.attempts: list[NormalizedOutboundEmail] = []

        async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail:
            self.attempts.append(reply)
            raise RuntimeError("smtp down")

    provider = ExplodingProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    agent = AssistantAgent()
    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path / "attachments",
        email_provider=provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=InMemoryMemoryAdapter(),
        agent=agent,
        projector=EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs"),
        recorder=RunRecorder(sqlite_session_factory),
        budget_governor=BudgetGovernor(sqlite_session_factory),
        envelope_builder=ReplyEnvelopeBuilder(),
        message_id_factory=lambda: "<run-abc@x>",
        provider_message_id_factory=lambda: "prov-out-1",
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with (
        agent.override_model(_scope(), _failing_model()),
        pytest.raises(ValueError, match="exploded"),
    ):
        await runtime.execute_run(run_id)

    # Both sends were attempted despite the first raising.
    assert len(provider.attempts) == 2
    assert provider.attempts[0].to_emails == ["mum@example.com"]
    assert provider.attempts[1].to_emails == ["admin@example.com"]

    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "failed"


def _slow_model(sleep_seconds: float = 5.0) -> FunctionModel:
    """Model that sleeps before responding so the runtime's timeout fires."""
    import asyncio as _asyncio

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await _asyncio.sleep(sleep_seconds)
        return ModelResponse(parts=[TextPart(content="too slow")])

    return FunctionModel(fn)


async def test_execute_run_enforces_run_timeout(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    import time

    import pytest

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path / "attachments",
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,
        agent=agent,
        projector=EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs"),
        recorder=RunRecorder(sqlite_session_factory),
        budget_governor=BudgetGovernor(sqlite_session_factory),
        envelope_builder=ReplyEnvelopeBuilder(),
        message_id_factory=lambda: "<run-abc@x>",
        provider_message_id_factory=lambda: "prov-out-1",
        run_timeout_seconds=1,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    start = time.monotonic()
    with (
        agent.override_model(_scope(), _slow_model(sleep_seconds=5.0)),
        pytest.raises(TimeoutError),
    ):
        await runtime.execute_run(run_id)
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, f"timeout did not fire fast enough: {elapsed:.2f}s"

    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "failed"
        assert run.error is not None
        assert "timeout" in run.error.lower() or "timed out" in run.error.lower()


async def test_execute_run_with_memory_disabled_skips_recall_and_curate(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """When `memory=None`, the runtime should still complete a run end-to-end:
    no recall, no RunMemoryRecall rows, no `memory_search` tool registered,
    no curate_memory defer scheduled.
    """
    from email_agent.db.models import RunMemoryRecall

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    email_provider = InMemoryEmailProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    agent = AssistantAgent(has_memory=False)

    captured_tool_names: list[set[str]] = []

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured_tool_names.append({t.name for t in info.function_tools})
        return ModelResponse(parts=[TextPart(content="hello back")])

    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path / "attachments",
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=None,
        agent=agent,
        projector=EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs"),
        # Production wiring (composition.make_runtime_from_settings) passes
        # curate_memory_defer=None when memory is None — mirror that here.
        recorder=RunRecorder(sqlite_session_factory, curate_memory_defer=None),
        budget_governor=BudgetGovernor(sqlite_session_factory),
        envelope_builder=ReplyEnvelopeBuilder(),
        message_id_factory=lambda: "<run-abc@x>",
        provider_message_id_factory=lambda: "prov-out-1",
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with agent.override_model(_scope(), FunctionModel(fn)):
        outcome = await runtime.execute_run(run_id)

    assert isinstance(outcome, Completed)

    # No memory_search tool reached the model.
    assert captured_tool_names, "model was never called"
    for tools in captured_tool_names:
        assert "memory_search" not in tools

    # No RunMemoryRecall rows persisted.
    async with sqlite_session_factory() as session:
        rows = (await session.execute(select(RunMemoryRecall))).scalars().all()
        assert rows == []

    # RunRecorder has no curate_memory_defer wired (mirrors production
    # composition for memory=None) — nothing to schedule, nothing fires.
    assert runtime._recorder._curate_memory_defer is None


async def test_curate_defer_not_scheduled_when_memory_disabled_in_composition(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,
):
    """composition.make_runtime_from_settings must not pass curate_memory_defer
    into RunRecorder when memory is None."""
    from email_agent.composition import make_runtime_from_settings
    from email_agent.config import Settings

    # Build a minimal Settings via env. database_url, mailgun_*, fireworks_*,
    # cognee_* are all required SecretStr/str fields — set just enough.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/test")
    monkeypatch.setenv("MAILGUN_SIGNING_KEY", "x")
    monkeypatch.setenv("MAILGUN_API_KEY", "x")
    monkeypatch.setenv("MAILGUN_DOMAIN", "example.com")
    monkeypatch.setenv("MAILGUN_WEBHOOK_URL", "https://example.com/x")
    monkeypatch.setenv("FIREWORKS_API_KEY", "x")
    monkeypatch.setenv("COGNEE_LLM_API_KEY", "x")
    monkeypatch.setenv("COGNEE_EMBEDDING_API_KEY", "x")
    monkeypatch.setenv("MEMORY_ENABLED", "false")

    settings = Settings()  # ty: ignore[missing-argument]
    assert settings.memory_enabled is False

    runtime = make_runtime_from_settings(
        settings,
        sqlite_session_factory,
        email_provider=InMemoryEmailProvider(),
        workspace_provider=StaticWorkspaceProvider(AssistantWorkspace(InMemoryEnvironment())),
        use_real_model=False,
        use_real_memory=False,
        use_docker_sandbox=False,
        use_procrastinate=False,
    )
    # Memory stays None and the recorder has no curate defer.
    assert runtime._memory is None
    assert runtime._recorder._curate_memory_defer is None


@pytest.mark.xfail(
    reason="TestModel's default tool script now targets run_code under code mode.",
    strict=True,
)
async def test_execute_run_notifies_on_failure_after_agent_succeeded(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """If something blows up AFTER agent.run finished — e.g. envelope
    building, markdown rendering, mailgun send, or the recorder write —
    the user-facing apology and owner notification must still fire and
    the run must be recorded failed. Otherwise a misrendered body
    would silently drop the response without anyone knowing.
    """
    import pytest
    from pydantic_ai.models.test import TestModel

    from email_agent.models.email import NormalizedOutboundEmail, SentEmail

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        owner = await session.get(Owner, "o-1")
        assert owner is not None
        owner.email = "owner@example.com"
        await session.commit()

    class _RaisingProvider:
        def __init__(self) -> None:
            self.attempts: list[NormalizedOutboundEmail] = []

        async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail:
            self.attempts.append(reply)
            if len(self.attempts) == 1 and reply.in_reply_to_header is not None:
                # The first successful-reply send blows up after the agent run.
                raise RuntimeError("smtp explosion")
            # Subsequent sends (the end-user apology + owner notification)
            # succeed so the test can observe them.
            return SentEmail(
                provider_message_id=f"p{len(self.attempts)}",
                message_id_header=reply.message_id_header,
            )

    provider = _RaisingProvider()
    workspace = AssistantWorkspace(InMemoryEnvironment())
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=provider,  # ty: ignore[invalid-argument-type]
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=InMemoryMemoryAdapter(),
        agent=agent,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with (
        agent.override_model(_scope(), TestModel(custom_output_text="hello back")),
        pytest.raises(RuntimeError, match="smtp explosion"),
    ):
        await runtime.execute_run(run_id)

    # 1 attempted real reply (exploded) + 2 failure notifications.
    assert len(provider.attempts) == 3
    end_user_apology, owner_note = provider.attempts[1], provider.attempts[2]
    assert end_user_apology.to_emails == ["mum@example.com"]
    assert end_user_apology.in_reply_to_header is not None
    assert "smtp explosion" not in end_user_apology.body_text
    assert owner_note.to_emails == ["owner@example.com"]
    assert owner_note.in_reply_to_header is None
    assert "smtp explosion" in owner_note.body_text

    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "failed"
        assert "smtp explosion" in (run.error or "")
