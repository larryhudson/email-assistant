from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EmailThread,
    EndUser,
    MessageIndex,
    Owner,
    RunStep,
    UsageLedger,
)
from email_agent.domain.inbound_persister import persist_inbound
from email_agent.domain.run_recorder import CompletedRun, RunRecorder
from email_agent.models.agent import MeteredUsage, RunStepRecord, RunUsage
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import (
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
)


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
        tool_allowlist=("read",),
        budget_id="b-1",
        model_name="deepseek-flash",
        system_prompt="be kind",
    )


async def _seed_run(
    session: AsyncSession,
    *,
    tmp_path: Path,
    run_id: str = "r-1",
) -> tuple[str, str]:
    """Seed an assistant + inbound message + queued AgentRun. Returns (run_id, inbound_message_id)."""
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
            model="deepseek-flash",
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
    thread = EmailThread(
        id="t-1",
        assistant_id="a-1",
        end_user_id="u-1",
        root_message_id="<m1@x>",
        subject_normalized="Question",
    )
    session.add(thread)
    await session.flush()
    persisted = await persist_inbound(
        session,
        email=NormalizedInboundEmail(
            provider_message_id="prov-1",
            message_id_header="<m1@x>",
            from_email="mum@example.com",
            to_emails=["mum@assistants.example.com"],
            subject="Question?",
            body_text="hello",
            received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
        ),
        scope=_scope(),
        thread=thread,
        attachments_root=tmp_path,
    )
    session.add(
        AgentRun(
            id=run_id,
            assistant_id="a-1",
            thread_id="t-1",
            inbound_message_id=persisted.message.id,
            status="queued",
        )
    )
    await session.commit()
    return run_id, persisted.message.id


def _outbound(*, message_id: str = "<run-abc@x>") -> NormalizedOutboundEmail:
    return NormalizedOutboundEmail(
        from_email="mum@assistants.example.com",
        to_emails=["mum@example.com"],
        subject="Re: Question?",
        body_text="ok, will do",
        message_id_header=message_id,
        in_reply_to_header="<m1@x>",
        references_headers=["<m1@x>"],
    )


async def test_record_completion_writes_outbound_and_updates_run(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        run_id, _ = await _seed_run(session, tmp_path=tmp_path)

    recorder = RunRecorder(sqlite_session_factory)
    completed = CompletedRun(
        run_id=run_id,
        scope=_scope(),
        outbound=_outbound(),
        sent=SentEmail(
            provider_message_id="prov-out-1",
            message_id_header="<run-abc@x>",
        ),
        steps=[
            RunStepRecord(
                kind="model",
                input_summary="prompt",
                output_summary="reply",
                cost_usd=Decimal("0.02"),
            )
        ],
        usage=RunUsage(input_tokens=100, output_tokens=20, cost_usd=Decimal("0.03")),
    )

    await recorder.record_completion(completed)

    async with sqlite_session_factory() as session:
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
        out = outbound_msgs[0]
        assert out.message_id_header == "<run-abc@x>"
        assert out.in_reply_to_header == "<m1@x>"
        assert out.references_headers == ["<m1@x>"]

        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "completed"
        assert run.completed_at is not None
        assert run.reply_message_id == out.id
        assert run.error is None

        steps = (await session.execute(select(RunStep))).scalars().all()
        assert len(steps) == 1
        assert steps[0].cost_usd == Decimal("0.02")

        usage_rows = (await session.execute(select(UsageLedger))).scalars().all()
        assert len(usage_rows) == 1
        assert usage_rows[0].input_tokens == 100
        assert usage_rows[0].output_tokens == 20

        index_rows = (
            (
                await session.execute(
                    select(MessageIndex).where(MessageIndex.message_id_header == "<run-abc@x>")
                )
            )
            .scalars()
            .all()
        )
        assert len(index_rows) == 1
        assert index_rows[0].thread_id == "t-1"


async def test_record_completion_persists_metered_tool_usage_separately(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        run_id, _ = await _seed_run(session, tmp_path=tmp_path)

    recorder = RunRecorder(sqlite_session_factory)
    await recorder.record_completion(
        CompletedRun(
            run_id=run_id,
            scope=_scope(),
            outbound=_outbound(),
            sent=SentEmail(provider_message_id="prov-out-1", message_id_header="<run-abc@x>"),
            steps=[
                RunStepRecord(
                    kind="tool:web_search",
                    input_summary='{"query": "x"}',
                    output_summary="results",
                    cost_usd=Decimal("0.0050"),
                )
            ],
            usage=RunUsage(input_tokens=100, output_tokens=20, cost_usd=Decimal("0.0350")),
            metered_usage=[
                MeteredUsage(
                    provider="brave",
                    model="web-search",
                    cost_usd=Decimal("0.0050"),
                    tool_name="web_search",
                )
            ],
        )
    )

    async with sqlite_session_factory() as session:
        usage_rows = (
            (await session.execute(select(UsageLedger).order_by(UsageLedger.provider)))
            .scalars()
            .all()
        )
        assert len(usage_rows) == 2
        brave = next(u for u in usage_rows if u.provider == "brave")
        model = next(u for u in usage_rows if u.provider == "openai-compat")
        assert brave.input_tokens == 0
        assert brave.output_tokens == 0
        assert brave.cost_usd == Decimal("0.0050")
        assert model.cost_usd == Decimal("0.0300")


async def test_record_completion_idempotent_on_duplicate(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        run_id, _ = await _seed_run(session, tmp_path=tmp_path)

    recorder = RunRecorder(sqlite_session_factory)
    completed = CompletedRun(
        run_id=run_id,
        scope=_scope(),
        outbound=_outbound(),
        sent=SentEmail(
            provider_message_id="prov-out-1",
            message_id_header="<run-abc@x>",
        ),
        steps=[],
        usage=RunUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal("0")),
    )

    await recorder.record_completion(completed)
    await recorder.record_completion(completed)  # duplicate

    async with sqlite_session_factory() as session:
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


async def test_record_failure_marks_run_failed(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        run_id, _ = await _seed_run(session, tmp_path=tmp_path)

    recorder = RunRecorder(sqlite_session_factory)
    await recorder.record_failure(run_id, error="model exploded")

    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "failed"
        assert run.error == "model exploded"
        assert run.completed_at is not None

        outbound_msgs = (
            (
                await session.execute(
                    select(EmailMessage).where(EmailMessage.direction == "outbound")
                )
            )
            .scalars()
            .all()
        )
        assert len(outbound_msgs) == 0


async def test_record_failure_persists_partial_usage_and_steps_when_provided(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """Partial state captured before the agent crashed must land in the DB
    so the budget cap stays accurate and the admin trace shows progress.
    """
    from email_agent.db.models import RunStep, UsageLedger
    from email_agent.models.agent import RunStepRecord, RunUsage

    async with sqlite_session_factory() as session:
        run_id, _ = await _seed_run(session, tmp_path=tmp_path)

    recorder = RunRecorder(sqlite_session_factory)
    partial_usage = RunUsage(
        input_tokens=120,
        output_tokens=45,
        cost_usd=Decimal("0.0034"),
    )
    partial_steps = [
        RunStepRecord(kind="tool:read", input_summary='{"path": "x"}', output_summary="x-body"),
        RunStepRecord(kind="model", input_summary="", output_summary="<tool plan>"),
    ]
    await recorder.record_failure(
        run_id,
        error="model boom",
        usage=partial_usage,
        steps=partial_steps,
        model_name="test-model",
    )

    async with sqlite_session_factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        assert run.status == "failed"

        steps = (await session.execute(select(RunStep))).scalars().all()
        assert sorted(s.kind for s in steps) == ["model", "tool:read"]

        ledger = (await session.execute(select(UsageLedger))).scalars().all()
        assert len(ledger) == 1
        assert ledger[0].input_tokens == 120
        assert ledger[0].output_tokens == 45
        assert ledger[0].model == "test-model"


async def test_record_failure_omits_ledger_when_no_usage(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """A run that crashed before any model response was completed has zero
    captured usage — no UsageLedger row should be written for it.
    """
    from email_agent.db.models import RunStep, UsageLedger
    from email_agent.models.agent import RunUsage

    async with sqlite_session_factory() as session:
        run_id, _ = await _seed_run(session, tmp_path=tmp_path)

    recorder = RunRecorder(sqlite_session_factory)
    await recorder.record_failure(
        run_id,
        error="boom before any response",
        usage=RunUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal("0")),
        steps=[],
        model_name="test-model",
    )

    async with sqlite_session_factory() as session:
        steps = (await session.execute(select(RunStep))).scalars().all()
        assert steps == []
        ledger = (await session.execute(select(UsageLedger))).scalars().all()
        assert ledger == []


async def test_record_completion_calls_curate_memory_defer(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """After commit, RunRecorder should invoke curate_memory_defer with the
    run's identity so a worker can pick up curate_memory."""
    async with sqlite_session_factory() as session:
        run_id, _ = await _seed_run(session, tmp_path=tmp_path)

    deferred: list[dict[str, str]] = []

    async def fake_defer(*, assistant_id: str, thread_id: str, run_id: str) -> None:
        deferred.append({"assistant_id": assistant_id, "thread_id": thread_id, "run_id": run_id})

    recorder = RunRecorder(sqlite_session_factory, curate_memory_defer=fake_defer)
    await recorder.record_completion(
        CompletedRun(
            run_id=run_id,
            scope=_scope(),
            outbound=_outbound(),
            sent=SentEmail(provider_message_id="prov-out-1", message_id_header="<run-abc@x>"),
            steps=[],
            usage=RunUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal("0")),
        )
    )

    assert len(deferred) == 1
    assert deferred[0]["assistant_id"] == "a-1"
    assert deferred[0]["thread_id"] == "t-1"
    assert deferred[0]["run_id"] == run_id


async def test_record_failure_does_not_call_curate_memory_defer(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """Failed runs have no outbound to curate; the defer should be skipped."""
    async with sqlite_session_factory() as session:
        run_id, _ = await _seed_run(session, tmp_path=tmp_path)

    deferred: list[dict] = []

    async def fake_defer(**kwargs) -> None:
        deferred.append(kwargs)

    recorder = RunRecorder(sqlite_session_factory, curate_memory_defer=fake_defer)
    await recorder.record_failure(run_id, error="boom")

    assert deferred == []


@pytest.mark.parametrize("missing_run_id", ["does-not-exist"])
async def test_record_completion_raises_when_run_missing(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    missing_run_id: str,
):
    recorder = RunRecorder(sqlite_session_factory)
    with pytest.raises(LookupError):
        await recorder.record_completion(
            CompletedRun(
                run_id=missing_run_id,
                scope=_scope(),
                outbound=_outbound(),
                sent=SentEmail(provider_message_id="x", message_id_header="<x>"),
                steps=[],
                usage=RunUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal("0")),
            )
        )
