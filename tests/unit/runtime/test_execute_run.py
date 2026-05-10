from datetime import UTC, datetime
from pathlib import Path

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
from email_agent.sandbox.inmemory import InMemorySandbox


async def _seed_assistant(session: AsyncSession) -> None:
    session.add(Owner(id="o-1", name="Larry"))
    session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
    session.add(
        Budget(
            id="b-1",
            assistant_id="a-1",
            monthly_limit_cents=1000,
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
    sandbox: InMemorySandbox,
    memory: InMemoryMemoryAdapter,
    agent: AssistantAgent,
) -> AssistantRuntime:
    return AssistantRuntime(
        session_factory,
        attachments_root=tmp_path / "attachments",
        email_provider=email_provider,
        sandbox=sandbox,
        memory=memory,
        agent=agent,
        projector=EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs"),
        recorder=RunRecorder(session_factory),
        budget_governor=BudgetGovernor(session_factory),
        envelope_builder=ReplyEnvelopeBuilder(),
        message_id_factory=lambda: "<run-abc@assistants.example.com>",
        provider_message_id_factory=lambda: "prov-out-1",
    )


async def _run_id_for(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    async with session_factory() as session:
        row = (await session.execute(select(AgentRun))).scalar_one()
        return row.id


async def test_execute_run_sends_reply_and_records_completion(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    email_provider = InMemoryEmailProvider()
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        sandbox=sandbox,
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
    assert sent.body_text == "Re: thanks!"
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
                cost_cents=1000,
                budget_period="2026-05",
                created_at=datetime(2026, 5, 5, tzinfo=UTC),
            )
        )
        # The seed run's UsageLedger references a non-existent run_id; SQLite
        # doesn't enforce FK by default in our test setup.
        await session.commit()

    email_provider = InMemoryEmailProvider()
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        sandbox=sandbox,
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

    # Sandbox was never touched.
    assert sandbox._started == set() or "a-1" not in getattr(sandbox, "_started", set())

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
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = _build_runtime(
        sqlite_session_factory,
        tmp_path=tmp_path,
        email_provider=email_provider,
        sandbox=sandbox,
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

    assert email_provider.sent == []

    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "failed"
        assert run.error is not None
        assert "exploded" in run.error


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
    sandbox = InMemorySandbox()
    memory = InMemoryMemoryAdapter()
    agent = AssistantAgent()
    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path / "attachments",
        email_provider=email_provider,
        sandbox=sandbox,
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
