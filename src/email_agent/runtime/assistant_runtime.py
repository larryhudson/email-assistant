import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pydantic_ai.models import Model

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailAttachmentRow,
    EmailMessage,
    EmailThread,
    EndUser,
    Owner,
)
from email_agent.domain.budget_governor import (
    Allow,
    BudgetGovernor,
    BudgetLimitReply,
)
from email_agent.domain.budget_reply import build_budget_limit_reply
from email_agent.domain.inbound_persister import persist_inbound
from email_agent.domain.reply_envelope import ReplyEnvelopeBuilder
from email_agent.domain.router import (
    AssistantRouter,
    Routed,
    RouteRejection,
    RouteRejectionReason,
)
from email_agent.domain.run_recorder import CompletedRun, RunRecorder
from email_agent.domain.thread_resolver import ThreadResolver
from email_agent.domain.workspace_projector import EmailWorkspaceProjector
from email_agent.models.agent import AgentDeps
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
)
from email_agent.models.sandbox import (
    PendingAttachment,
    ProjectedFile,
    ToolCall,
    ToolResult,
)


class _EmailProviderLike(Protocol):
    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail: ...


class _SandboxLike(Protocol):
    async def ensure_started(self, assistant_id: str) -> None: ...
    async def project_emails(self, assistant_id: str, files: list[ProjectedFile]) -> None: ...
    async def project_attachments(
        self, assistant_id: str, run_id: str, files: list[ProjectedFile]
    ) -> None: ...
    async def run_tool(self, assistant_id: str, run_id: str, call: ToolCall) -> ToolResult: ...
    async def read_attachment_out(self, assistant_id: str, run_id: str, path: str) -> bytes: ...


class _MemoryLike(Protocol):
    async def search(self, assistant_id: str, query: str): ...

    async def recall(self, assistant_id: str, thread_id: str, query: str): ...


@dataclass(frozen=True)
class Accepted:
    """Inbound persisted; webhook should return 200."""

    assistant_id: str
    thread_id: str
    message_id: str
    created: bool


@dataclass(frozen=True)
class Dropped:
    """Inbound rejected before persistence; webhook should still return 200."""

    reason: RouteRejectionReason
    detail: str


@dataclass(frozen=True)
class Completed:
    run_id: str
    sent: SentEmail


@dataclass(frozen=True)
class BudgetLimited:
    run_id: str
    sent: SentEmail


@dataclass(frozen=True)
class Failed:
    run_id: str
    error: str


AcceptOutcome = Accepted | Dropped
RunOutcome = Completed | BudgetLimited | Failed


class AssistantRuntime:
    """Top-level orchestrator with two entry points.

    `accept_inbound` is the webhook fast path: route, persist, queue an
    `agent_runs(status='queued')` row. `execute_run` is the worker entry
    point: budget gate → projector → sandbox → agent → reply → record.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        attachments_root: Path,
        # Below are required for execute_run; left optional so accept_inbound-only
        # callers (the webhook fast path) don't have to construct them.
        email_provider: _EmailProviderLike | None = None,
        sandbox: _SandboxLike | None = None,
        memory: _MemoryLike | None = None,
        agent: AssistantAgent | None = None,
        projector: EmailWorkspaceProjector | None = None,
        recorder: RunRecorder | None = None,
        budget_governor: BudgetGovernor | None = None,
        envelope_builder: ReplyEnvelopeBuilder | None = None,
        message_id_factory: Callable[[], str] | None = None,
        provider_message_id_factory: Callable[[], str] | None = None,
        run_timeout_seconds: float | None = None,
        model_factory: "Callable[[AssistantScope], Model] | None" = None,
        run_agent_defer: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._attachments_root = attachments_root
        self._router = AssistantRouter(session_factory)
        self._resolver = ThreadResolver(session_factory)
        self._email_provider = email_provider
        self._sandbox = sandbox
        self._memory = memory
        self._agent = agent
        self._projector = projector
        self._recorder = recorder or RunRecorder(session_factory)
        self._budget = budget_governor or BudgetGovernor(session_factory)
        self._envelope_builder = envelope_builder or ReplyEnvelopeBuilder()
        self._message_id_factory = message_id_factory or _default_message_id_factory
        self._provider_message_id_factory = (
            provider_message_id_factory or _default_provider_message_id_factory
        )
        self._run_timeout_seconds = run_timeout_seconds
        self._model_factory = model_factory
        self._run_agent_defer = run_agent_defer

    async def accept_inbound(self, email: NormalizedInboundEmail) -> AcceptOutcome:
        outcome = await self._router.resolve(email)
        if isinstance(outcome, RouteRejection):
            return Dropped(reason=outcome.reason, detail=outcome.detail)
        assert isinstance(outcome, Routed)
        scope = outcome.scope

        thread = await self._resolver.resolve(email, scope)

        async with self._session_factory() as session:
            attached_thread = await session.merge(thread)
            persisted = await persist_inbound(
                session,
                email=email,
                scope=scope,
                thread=attached_thread,
                attachments_root=self._attachments_root,
            )
            run = await _ensure_queued_run(
                session,
                assistant_id=scope.assistant_id,
                thread_id=attached_thread.id,
                inbound_message_id=persisted.message.id,
            )
            run_id = run.id
            await session.commit()

        # Enqueue ONLY for newly-persisted inbounds. persist_inbound +
        # _ensure_queued_run share a transaction, so created==True implies
        # the AgentRun is also new — duplicate webhook deliveries fall here
        # with created==False and skip the enqueue.
        if persisted.created and self._run_agent_defer is not None:
            await self._run_agent_defer(
                run_id=run_id,
                assistant_id=scope.assistant_id,
            )

        return Accepted(
            assistant_id=scope.assistant_id,
            thread_id=attached_thread.id,
            message_id=persisted.message.id,
            created=persisted.created,
        )

    async def execute_run(self, run_id: str) -> RunOutcome:
        if (
            self._email_provider is None
            or self._sandbox is None
            or self._memory is None
            or self._agent is None
            or self._projector is None
        ):
            raise RuntimeError(
                "execute_run requires email_provider, sandbox, memory, agent, "
                "and projector to be configured"
            )

        scope, _run, inbound, thread, messages, attachments = await self._load_run(run_id)
        inbound_email = _inbound_email_from_message(inbound)

        decision = await self._budget.decide(scope)
        if isinstance(decision, BudgetLimitReply):
            envelope = build_budget_limit_reply(
                inbound=inbound_email,
                scope=scope,
                decision=decision,
                message_id_factory=self._message_id_factory,
            )
            sent = await self._email_provider.send_reply(envelope)
            await self._record_budget_limited(run_id, scope, envelope, sent)
            return BudgetLimited(run_id=run_id, sent=sent)

        assert isinstance(decision, Allow)

        # Project the thread to the host inputs dir, then mirror into the sandbox.
        projection = self._projector.project(
            run_id=run_id,
            thread=thread,
            messages=messages,
            attachments=attachments,
            current_message_id=inbound.id,
        )
        projected_files = _read_projection_files(projection.run_inputs_dir)
        await self._sandbox.ensure_started(scope.assistant_id)
        await self._sandbox.project_emails(scope.assistant_id, projected_files)

        deps = AgentDeps(
            assistant_id=scope.assistant_id,
            run_id=run_id,
            thread_id=thread.id,
            sandbox=self._sandbox,
            memory=self._memory,
            pending_attachments=[],
        )

        # Pre-call recall once with the inbound body (truncated) as the query.
        # Reliable beats clever — gives the model prior context without it
        # having to decide to call memory_search first.
        recall_query = (inbound_email.body_text or "")[:2000]
        memory_context = await self._memory.recall(
            assistant_id=scope.assistant_id,
            thread_id=thread.id,
            query=recall_query,
        )
        if memory_context.memories:
            memory_block = "\n\nRecalled memory:\n" + "\n".join(
                f"- {m.content}" for m in memory_context.memories
            )
        else:
            memory_block = ""

        prompt = (
            f"A new inbound email has arrived. Read it from {projection.current_message_path!r} "
            f"using the `read` tool. Your final response (a plain string returned from this run) "
            f"becomes the body of the reply email — do NOT write the reply to disk, and do NOT "
            f"modify anything under emails/ (that directory is the read-only thread history). "
            f"Use `write`/`edit`/`bash` only if you need scratch files under other paths. "
            f"Use `memory_search` to look up prior context. Use `attach_file` only if you "
            f"genuinely need to attach a generated artefact." + memory_block
        )

        # If a model_factory is wired in (production), apply it for the run;
        # otherwise rely on a test having called agent.override_model itself.
        model_override = (
            self._agent.override_model(scope, self._model_factory(scope))
            if self._model_factory is not None
            else contextlib.nullcontext()
        )

        try:
            with model_override:
                if self._run_timeout_seconds is not None:
                    agent_result = await asyncio.wait_for(
                        self._agent.run(scope, prompt=prompt, deps=deps),
                        timeout=self._run_timeout_seconds,
                    )
                else:
                    agent_result = await self._agent.run(scope, prompt=prompt, deps=deps)
        except TimeoutError:
            await self._recorder.record_failure(
                run_id,
                error=f"run timed out after {self._run_timeout_seconds}s",
            )
            raise
        except Exception as exc:
            await self._recorder.record_failure(run_id, error=str(exc))
            raise

        attachment_models = await _read_attachments_out(
            self._sandbox,
            scope.assistant_id,
            run_id,
            deps.pending_attachments,
        )

        envelope = self._envelope_builder.build(
            inbound=inbound_email,
            from_email=scope.inbound_address,
            body_text=agent_result.body,
            attachments=attachment_models,
            message_id_factory=self._message_id_factory,
        )

        sent = await self._email_provider.send_reply(envelope)

        await self._recorder.record_completion(
            CompletedRun(
                run_id=run_id,
                scope=scope,
                outbound=envelope,
                sent=sent,
                steps=agent_result.steps,
                usage=agent_result.usage,
            )
        )

        return Completed(run_id=run_id, sent=sent)

    async def _load_run(
        self, run_id: str
    ) -> tuple[
        AssistantScope,
        AgentRun,
        EmailMessage,
        EmailThread,
        list[EmailMessage],
        list[EmailAttachmentRow],
    ]:
        async with self._session_factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent_run {run_id} not found")

            assistant = await session.get(Assistant, run.assistant_id)
            if assistant is None:
                raise LookupError(f"assistant {run.assistant_id} not found")
            end_user = await session.get(EndUser, assistant.end_user_id)
            assert end_user is not None
            owner = await session.get(Owner, end_user.owner_id)
            assert owner is not None
            scope_row = (
                await session.execute(
                    select(AssistantScopeRow).where(AssistantScopeRow.assistant_id == assistant.id)
                )
            ).scalar_one()
            await session.get(Budget, scope_row.budget_id)
            scope = AssistantScope.from_rows(
                owner=owner,
                end_user=end_user,
                assistant=assistant,
                scope_row=scope_row,
            )

            inbound = await session.get(EmailMessage, run.inbound_message_id)
            assert inbound is not None
            thread = await session.get(EmailThread, run.thread_id)
            assert thread is not None
            messages = (
                (
                    await session.execute(
                        select(EmailMessage)
                        .where(EmailMessage.thread_id == run.thread_id)
                        .order_by(EmailMessage.created_at, EmailMessage.id)
                    )
                )
                .scalars()
                .all()
            )
            attachments = (
                (
                    await session.execute(
                        select(EmailAttachmentRow).where(
                            EmailAttachmentRow.message_id.in_([m.id for m in messages])
                        )
                    )
                )
                .scalars()
                .all()
            )

            # Detach so callers can use them after the session closes.
            session.expunge_all()
            return scope, run, inbound, thread, list(messages), list(attachments)

    async def _record_budget_limited(
        self,
        run_id: str,
        scope: AssistantScope,
        envelope: NormalizedOutboundEmail,
        sent: SentEmail,
    ) -> None:
        await self._recorder.record_completion(
            CompletedRun(
                run_id=run_id,
                scope=scope,
                outbound=envelope,
                sent=sent,
                steps=[],
                usage=_zero_usage(),
            )
        )
        # Mark the run distinctly as budget_limited (record_completion set
        # status='completed' first, but the design wants a separate label).
        async with self._session_factory() as session:
            run = await session.get(AgentRun, run_id)
            assert run is not None
            run.status = "budget_limited"
            await session.commit()


# --- helpers ---------------------------------------------------------------


def _read_projection_files(run_inputs_dir: Path) -> list[ProjectedFile]:
    files: list[ProjectedFile] = []
    emails_root = run_inputs_dir / "emails"
    if not emails_root.exists():
        return files
    for path in emails_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(run_inputs_dir)
        files.append(ProjectedFile(path=str(rel), content=path.read_bytes()))
    return files


async def _read_attachments_out(
    sandbox: _SandboxLike,
    assistant_id: str,
    run_id: str,
    pending: list[PendingAttachment],
) -> list[EmailAttachment]:
    out: list[EmailAttachment] = []
    for att in pending:
        data = await sandbox.read_attachment_out(assistant_id, run_id, att.sandbox_path)
        out.append(
            EmailAttachment(
                filename=att.filename,
                content_type="application/octet-stream",
                size_bytes=len(data),
                data=data,
            )
        )
    return out


def _inbound_email_from_message(message: EmailMessage) -> NormalizedInboundEmail:
    """Reconstruct a NormalizedInboundEmail from the persisted row.

    Only the fields the envelope builder needs are populated.
    """
    return NormalizedInboundEmail(
        provider_message_id=message.provider_message_id,
        message_id_header=message.message_id_header,
        in_reply_to_header=message.in_reply_to_header,
        references_headers=list(message.references_headers or []),
        from_email=message.from_email,
        to_emails=list(message.to_emails),
        subject=message.subject,
        body_text=message.body_text,
        received_at=datetime.now(UTC),
    )


def _zero_usage():
    from decimal import Decimal as _Decimal

    from email_agent.models.agent import RunUsage

    return RunUsage(input_tokens=0, output_tokens=0, cost_usd=_Decimal("0"))


def _default_message_id_factory() -> str:
    return f"<run-{uuid.uuid4().hex[:12]}@email-agent>"


def _default_provider_message_id_factory() -> str:
    return f"prov-{uuid.uuid4().hex[:12]}"


async def _ensure_queued_run(
    session: AsyncSession,
    *,
    assistant_id: str,
    thread_id: str,
    inbound_message_id: str,
) -> AgentRun:
    """Idempotent AgentRun(status='queued') row keyed on inbound_message_id."""
    existing = (
        await session.execute(
            select(AgentRun).where(AgentRun.inbound_message_id == inbound_message_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    run = AgentRun(
        id=f"r-{uuid.uuid4().hex[:8]}",
        assistant_id=assistant_id,
        thread_id=thread_id,
        inbound_message_id=inbound_message_id,
        status="queued",
    )
    session.add(run)
    await session.flush()
    return run
