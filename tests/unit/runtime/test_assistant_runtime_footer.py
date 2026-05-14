from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    Budget,
    EndUser,
    Owner,
)
from email_agent.domain.budget_governor import BudgetGovernor
from email_agent.domain.reply_envelope import ReplyEnvelopeBuilder
from email_agent.domain.run_footer import FOOTER_MARKER
from email_agent.domain.run_recorder import RunRecorder
from email_agent.domain.workspace_projector import EmailWorkspaceProjector
from email_agent.mail.inmemory import InMemoryEmailProvider
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import NormalizedInboundEmail
from email_agent.runtime.assistant_runtime import AssistantRuntime, Completed
from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace
from email_agent.sandbox.workspace_provider import StaticWorkspaceProvider


async def _seed_assistant(session: AsyncSession) -> None:
    session.add(Owner(id="o-1", name="Larry"))
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
            tool_allowlist=["read", "write", "edit", "bash", "memory_search", "attach_file"],
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


def _instant_reply() -> FunctionModel:
    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="Re: thanks!")])

    return FunctionModel(fn)


async def _run_id_for(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        row = (await session.execute(select(AgentRun))).scalar_one()
        return row.id


def _build_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tmp_path: Path,
    email_provider: InMemoryEmailProvider,
    workspace: AssistantWorkspace,
    memory: InMemoryMemoryAdapter,
    agent: AssistantAgent,
    admin_base_url: str | None = None,
) -> AssistantRuntime:
    return AssistantRuntime(
        session_factory,
        attachments_root=tmp_path / "attachments",
        email_provider=email_provider,
        workspace_provider=StaticWorkspaceProvider(workspace),
        memory=memory,
        agent=agent,
        projector=EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs"),
        recorder=RunRecorder(session_factory),
        budget_governor=BudgetGovernor(session_factory),
        envelope_builder=ReplyEnvelopeBuilder(),
        message_id_factory=lambda: "<run-abc@assistants.example.com>",
        provider_message_id_factory=lambda: "prov-out-1",
        admin_base_url=admin_base_url,
    )


async def test_execute_run_appends_footer_with_marker_and_run_id(
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
        workspace=workspace,
        memory=memory,
        agent=agent,
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with agent.override_model(_scope(), _instant_reply()):
        outcome = await runtime.execute_run(run_id)

    assert isinstance(outcome, Completed)
    assert len(email_provider.sent) == 1
    sent = email_provider.sent[0]

    assert sent.body_text.startswith("Re: thanks!")
    assert FOOTER_MARKER in sent.body_text
    assert f"Run: {run_id}" in sent.body_text
    assert "Tokens:" in sent.body_text
    assert "Cost:" in sent.body_text
    # No admin_base_url → no Admin: link.
    assert "Admin:" not in sent.body_text

    assert sent.body_html is not None
    assert FOOTER_MARKER in sent.body_html


async def test_execute_run_footer_includes_admin_link_when_configured(
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
        workspace=workspace,
        memory=memory,
        agent=agent,
        admin_base_url="https://agent.example.com",
    )

    await runtime.accept_inbound(_inbound())
    run_id = await _run_id_for(sqlite_session_factory)

    with agent.override_model(_scope(), _instant_reply()):
        await runtime.execute_run(run_id)

    sent = email_provider.sent[0]
    assert f"https://agent.example.com/admin/runs/{run_id}" in sent.body_text
