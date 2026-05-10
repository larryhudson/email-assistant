import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    AgentRun,
    EmailMessage,
    MessageIndex,
    RunStep,
    UsageLedger,
)
from email_agent.models.agent import RunStepRecord, RunUsage
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import NormalizedOutboundEmail, SentEmail


@dataclass(frozen=True)
class CompletedRun:
    """Inputs for `RunRecorder.record_completion`.

    Bundles the bits the runtime collects after a successful agent run: the
    outbound envelope it built, the provider's send receipt, the trace of
    run_steps for the admin UI, and the token/cost usage from the model.
    """

    run_id: str
    scope: AssistantScope
    outbound: NormalizedOutboundEmail
    sent: SentEmail
    steps: list[RunStepRecord]
    usage: RunUsage


class RunRecorder:
    """Persists the result of a completed (or failed) agent run.

    Single transaction per call so partial state can't leak. Idempotent on
    duplicate `(assistant_id, provider_message_id)` for the outbound message
    (relies on the unique constraint on `email_messages`).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_completion(self, completed: CompletedRun) -> None:
        async with self._session_factory() as session:
            run = await session.get(AgentRun, completed.run_id)
            if run is None:
                raise LookupError(f"agent_run {completed.run_id} not found")

            outbound = await _upsert_outbound_message(
                session,
                run=run,
                scope=completed.scope,
                envelope=completed.outbound,
                sent=completed.sent,
            )

            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            run.reply_message_id = outbound.id
            run.error = None

            for step in completed.steps:
                session.add(
                    RunStep(
                        id=f"s-{uuid.uuid4().hex[:8]}",
                        run_id=run.id,
                        kind=step.kind,
                        input_summary=step.input_summary,
                        output_summary=step.output_summary,
                        cost_usd=step.cost_usd,
                    )
                )

            session.add(
                UsageLedger(
                    id=f"u-{uuid.uuid4().hex[:8]}",
                    assistant_id=run.assistant_id,
                    run_id=run.id,
                    provider="openai-compat",
                    model=completed.scope.model_name,
                    input_tokens=completed.usage.input_tokens,
                    output_tokens=completed.usage.output_tokens,
                    cost_usd=completed.usage.cost_usd,
                    budget_period=datetime.now(UTC).strftime("%Y-%m"),
                )
            )

            await session.commit()

    async def record_failure(self, run_id: str, *, error: str) -> None:
        async with self._session_factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent_run {run_id} not found")

            run.status = "failed"
            run.error = error
            run.completed_at = datetime.now(UTC)
            await session.commit()


async def _upsert_outbound_message(
    session: AsyncSession,
    *,
    run: AgentRun,
    scope: AssistantScope,
    envelope: NormalizedOutboundEmail,
    sent: SentEmail,
) -> EmailMessage:
    """Insert the outbound EmailMessage row, or return the existing duplicate.

    Idempotent on (assistant_id, provider_message_id).
    """
    existing = (
        await session.execute(
            select(EmailMessage).where(
                EmailMessage.assistant_id == scope.assistant_id,
                EmailMessage.provider_message_id == sent.provider_message_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    outbound = EmailMessage(
        id=f"m-{uuid.uuid4().hex[:8]}",
        thread_id=run.thread_id,
        assistant_id=scope.assistant_id,
        direction="outbound",
        provider_message_id=sent.provider_message_id,
        message_id_header=envelope.message_id_header,
        in_reply_to_header=envelope.in_reply_to_header,
        references_headers=list(envelope.references_headers),
        from_email=envelope.from_email,
        to_emails=list(envelope.to_emails),
        subject=envelope.subject,
        body_text=envelope.body_text,
    )
    session.add(outbound)
    await session.flush()

    session.add(
        MessageIndex(
            assistant_id=scope.assistant_id,
            message_id_header=envelope.message_id_header,
            thread_id=run.thread_id,
            provider_message_id=sent.provider_message_id,
        )
    )
    return outbound


__all__ = ["CompletedRun", "RunRecorder"]
