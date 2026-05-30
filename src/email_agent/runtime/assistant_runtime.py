import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from email_agent.document.port import DocumentToolsPort
    from email_agent.github.port import GitHubPort
    from email_agent.google_workspace.port import GoogleCalendarPort
    from email_agent.pdf.port import PdfRenderPort

from pydantic_ai.messages import ModelMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.agent.history import deserialize_message_history
from email_agent.agent.run_context import RunContextAssembler
from email_agent.agent.toolset import AgentToolset
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
    RunMemoryRecall,
    ScheduledTaskFireRow,
    ScheduledTaskRow,
)
from email_agent.domain.budget_governor import (
    Allow,
    BudgetGovernor,
    BudgetLimitReply,
)
from email_agent.domain.budget_reply import build_budget_limit_reply
from email_agent.domain.error_envelope import (
    build_end_user_error_envelope,
    build_owner_error_envelope,
)
from email_agent.domain.inbound_persister import persist_inbound
from email_agent.domain.participants import render_participants_block
from email_agent.domain.reply_envelope import ReplyEnvelopeBuilder, RunFooterContext
from email_agent.domain.router import (
    AssistantRouter,
    Routed,
    RouteRejection,
    RouteRejectionReason,
)
from email_agent.domain.run_footer import strip_footer
from email_agent.domain.run_recorder import CompletedRun, RunRecorder
from email_agent.domain.thread_resolver import ThreadResolver
from email_agent.domain.workspace_projector import EmailWorkspaceProjector, ProjectionResult
from email_agent.models.agent import AgentDeps, AgentRunError, MeteredUsage
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
)
from email_agent.models.memory import Memory
from email_agent.models.sandbox import PendingAttachment, ProjectedFile
from email_agent.models.scheduled import ScheduledTaskKind, ScheduledTaskStatus
from email_agent.sandbox.skills import (
    SYSTEM_PROMPT_GUIDANCE,
    render_context_block,
    render_identity_block,
    render_skills_block,
)
from email_agent.sandbox.source_projection import DEFAULT_PROJECT_ROOT
from email_agent.sandbox.workspace import AssistantWorkspace
from email_agent.sandbox.workspace_provider import WorkspaceProvider
from email_agent.scheduled.service import ScheduledTaskService
from email_agent.search.port import SearchPort

_log = logging.getLogger(__name__)


class _EmailProviderLike(Protocol):
    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail: ...


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
    run_id: str | None = None


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
class QuietExited:
    run_id: str


@dataclass(frozen=True)
class Failed:
    run_id: str
    error: str


class RuntimeScheduledDirectSender:
    def __init__(self, runtime: "AssistantRuntime") -> None:
        self._runtime = runtime

    async def send(
        self,
        *,
        assistant_id: str,
        to_email: str,
        subject: str,
        body_text: str,
    ) -> None:
        await self._runtime.send_scheduled_direct_email(
            assistant_id=assistant_id,
            to_email=to_email,
            subject=subject,
            body_text=body_text,
        )


AcceptOutcome = Accepted | Dropped
RunOutcome = Completed | BudgetLimited | QuietExited | Failed


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
        workspace_provider: WorkspaceProvider | None = None,
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
        scheduled_tasks: ScheduledTaskService | None = None,
        search: SearchPort | None = None,
        pdf_renderer: "PdfRenderPort | None" = None,
        document_tools: "DocumentToolsPort | None" = None,
        github: "GitHubPort | None" = None,
        google_calendar: "GoogleCalendarPort | None" = None,
        admin_base_url: str | None = None,
        assistant_tools_base_url: str = "http://assistant-tools",
        assistant_tools_token: str | None = None,
        assistant_surface_base_url_template: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._attachments_root = attachments_root
        self._router = AssistantRouter(session_factory)
        self._resolver = ThreadResolver(session_factory)
        self._email_provider = email_provider
        self._workspace_provider = workspace_provider
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
        self._scheduled_tasks = scheduled_tasks or ScheduledTaskService(session_factory)
        self._search = search
        self._pdf_renderer = pdf_renderer
        self._document_tools = document_tools
        self._github = github
        self._google_calendar = google_calendar
        self._context_assembler = RunContextAssembler()
        self._admin_base_url = admin_base_url
        self._assistant_tools_base_url = assistant_tools_base_url
        self._assistant_tools_token = assistant_tools_token
        self._assistant_surface_base_url_template = assistant_surface_base_url_template

    @property
    def scheduled_tasks(self) -> ScheduledTaskService:
        return self._scheduled_tasks

    @property
    def workspace_provider(self) -> WorkspaceProvider | None:
        return self._workspace_provider

    async def send_scheduled_direct_email(
        self,
        *,
        assistant_id: str,
        to_email: str,
        subject: str,
        body_text: str,
    ) -> None:
        if self._email_provider is None:
            raise RuntimeError("scheduled direct email requires email_provider to be configured")
        async with self._session_factory() as session:
            assistant = await session.get(Assistant, assistant_id)
            if assistant is None:
                raise LookupError(f"assistant {assistant_id} not found")
            from_email = assistant.inbound_address

        envelope = NormalizedOutboundEmail(
            from_email=from_email,
            to_emails=[to_email],
            subject=subject,
            body_text=body_text,
            message_id_header=self._message_id_factory(),
        )
        await self._email_provider.send_reply(envelope)

    async def accept_inbound(self, email: NormalizedInboundEmail) -> AcceptOutcome:
        # Drop our own outbound footer if a reply quotes it. Done here, at the
        # single runtime seam, so every adapter (Mailgun, eml, future ones)
        # stays a faithful transport — the domain owns marker semantics.
        email = email.model_copy(update={"body_text": strip_footer(email.body_text)})
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

        if persisted.created and not email.provider_message_id.startswith("sched-"):
            await self._reset_scheduled_unanswered_counters(scope.assistant_id)

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
            run_id=run_id,
        )

    async def accept_surface_action(
        self,
        *,
        assistant_id: str,
        subject: str,
        body_text: str,
        provider_message_id: str,
        message_id_header: str,
    ) -> AcceptOutcome:
        outcome = await self._router.resolve_assistant_id(assistant_id)
        if isinstance(outcome, RouteRejection):
            return Dropped(reason=outcome.reason, detail=outcome.detail)
        assert isinstance(outcome, Routed)
        scope = outcome.scope

        existing_run_id = await self._find_run_for_provider_message_id(
            assistant_id=scope.assistant_id,
            provider_message_id=provider_message_id,
        )
        if existing_run_id is not None:
            async with self._session_factory() as session:
                message = (
                    await session.execute(
                        select(EmailMessage).where(
                            EmailMessage.assistant_id == scope.assistant_id,
                            EmailMessage.provider_message_id == provider_message_id,
                        )
                    )
                ).scalar_one()
            return Accepted(
                assistant_id=scope.assistant_id,
                thread_id=message.thread_id,
                message_id=message.id,
                created=False,
                run_id=existing_run_id,
            )

        email = NormalizedInboundEmail(
            provider_message_id=provider_message_id,
            message_id_header=message_id_header,
            from_email=scope.owner_email or scope.inbound_address,
            to_emails=[scope.inbound_address],
            subject=subject,
            body_text=body_text,
            received_at=datetime.now(UTC),
        )

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
            run_id=run_id,
        )

    async def _find_run_for_provider_message_id(
        self,
        *,
        assistant_id: str,
        provider_message_id: str,
    ) -> str | None:
        async with self._session_factory() as session:
            stmt = (
                select(AgentRun.id)
                .join(EmailMessage, EmailMessage.id == AgentRun.inbound_message_id)
                .where(
                    EmailMessage.assistant_id == assistant_id,
                    EmailMessage.provider_message_id == provider_message_id,
                )
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def execute_run(self, run_id: str) -> RunOutcome:
        if (
            self._email_provider is None
            or self._workspace_provider is None
            or self._agent is None
            or self._projector is None
        ):
            raise RuntimeError(
                "execute_run requires email_provider, workspace_provider, agent, "
                "and projector to be configured"
            )

        (
            scope,
            _run,
            inbound,
            thread,
            threads,
            messages,
            attachments,
        ) = await self._load_run(run_id)
        await self._recorder.mark_running(run_id)
        workspace = await self._workspace_provider.get_workspace(scope.assistant_id)
        await workspace.write_platform_environment(
            assistant_id=scope.assistant_id,
            assistant_tools_base_url=self._assistant_tools_base_url,
            assistant_surface_base_url=self._assistant_surface_base_url(scope.assistant_id),
            assistant_tools_token=self._assistant_tools_token,
        )
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

        # Project every thread this assistant has with the end-user, so the
        # agent can read prior conversations as context, not just the current
        # one. The bind-mount stays read-only.
        projection = self._projector.project(
            run_id=run_id,
            threads=threads,
            messages=messages,
            attachments=attachments,
            current_thread_id=thread.id,
            current_message_id=inbound.id,
        )
        await _project_workspace_emails(workspace, projection)
        await workspace.ensure_starter_files()
        await workspace.project_source(DEFAULT_PROJECT_ROOT)
        skills = await workspace.load_skills()
        skills_block = render_skills_block(skills)
        context_block = render_context_block(await workspace.read_context())
        identity_block = render_identity_block(await workspace.read_identity())
        _log.info(
            "run %s loaded %d skill(s) into prompt: %s",
            run_id,
            len(skills),
            ",".join(s.name for s in skills) or "<none>",
        )

        # Pre-call recall once with the inbound body (truncated) as the query.
        # Reliable beats clever — gives the model prior context without it
        # having to decide to call memory_search first. When the memory
        # layer is disabled (self._memory is None), skip recall + persist
        # and feed the assembler an empty memories list.
        recalled_memories: list[Memory] = []
        if self._memory is not None:
            recall_query = (inbound_email.body_text or "")[:2000]
            memory_context = await self._memory.recall(
                assistant_id=scope.assistant_id,
                thread_id=thread.id,
                query=recall_query,
            )
            recalled_memories = list(memory_context.memories)
            await self._persist_memory_recalls(run_id, recalled_memories)
        prompt_context = self._context_assembler.build(
            current_message_path=projection.current_message_path,
            memories=recalled_memories,
            memory_enabled=self._memory is not None,
        )
        prompt = prompt_context.prompt

        # Render the participants block from scope so identity (owner +
        # end-user emails) flows in from data. Stored alongside the other
        # dynamic blocks in the persisted prompt for admin-UI visibility.
        participants_block = render_participants_block(
            owner_email=scope.owner_email,
            end_user_email=scope.end_user_email,
        )

        # Persist the fully-assembled system prompt + user prompt on the run
        # row so the admin UI can show exactly what the model saw. System
        # order mirrors assistant_agent._build_agent: workspace guidance,
        # then the dynamic identity + context + participants + skills blocks.
        full_system_prompt = "\n\n".join(
            part
            for part in [
                SYSTEM_PROMPT_GUIDANCE.strip(),
                identity_block.strip(),
                context_block.strip(),
                participants_block.strip(),
                skills_block.strip(),
            ]
            if part
        )
        async with self._session_factory() as session:
            run_row = await session.get(AgentRun, run_id)
            if run_row is not None:
                run_row.system_prompt = full_system_prompt
                run_row.user_prompt = prompt
                await session.commit()
        pending_attachments: list[PendingAttachment] = []
        metered_usage: list[MeteredUsage] = []
        deps = AgentDeps(
            assistant_id=scope.assistant_id,
            run_id=run_id,
            thread_id=thread.id,
            toolset=AgentToolset(
                assistant_id=scope.assistant_id,
                run_id=run_id,
                env=workspace.environment,
                workspace=workspace,
                memory=self._memory,
                pending_attachments=pending_attachments,
                metered_usage=metered_usage,
                search=self._search,
                scheduled_tasks=self._scheduled_tasks,
                pdf_renderer=self._pdf_renderer,
                document_tools=self._document_tools,
                github=self._github,
                google_calendar=self._google_calendar,
            ),
            pending_attachments=pending_attachments,
            metered_usage=metered_usage,
            record_step=lambda step: self._recorder.record_step(run_id, step),
            skills_block=skills_block,
            context_block=context_block,
            participants_block=participants_block,
            identity_block=identity_block,
        )

        # If a model_factory is wired in (production), apply it for the run;
        # otherwise rely on a test having called agent.override_model itself.
        model_override = (
            self._agent.override_model(scope, self._model_factory(scope))
            if self._model_factory is not None
            else contextlib.nullcontext()
        )

        prior_history = await self._load_prior_thread_history(
            assistant_id=scope.assistant_id,
            thread_id=thread.id,
            current_run_id=run_id,
        )

        try:
            with model_override:
                if self._run_timeout_seconds is not None:
                    agent_result = await asyncio.wait_for(
                        self._agent.run(
                            scope,
                            prompt=prompt,
                            deps=deps,
                            message_history=prior_history,
                        ),
                        timeout=self._run_timeout_seconds,
                    )
                else:
                    agent_result = await self._agent.run(
                        scope,
                        prompt=prompt,
                        deps=deps,
                        message_history=prior_history,
                    )
        except TimeoutError as exc:
            await self._recorder.record_failure(
                run_id,
                error=f"run timed out after {self._run_timeout_seconds}s",
            )
            await self._notify_run_failed(
                run_id=run_id,
                scope=scope,
                inbound_email=inbound_email,
                exception=exc,
            )
            raise
        except AgentRunError as wrapped:
            # Partial usage + steps were captured before the underlying
            # exception. Persist them so the admin trace + usage_ledger
            # reflect how far the run got. Then re-raise the ORIGINAL
            # exception so procrastinate sees the real failure mode.
            await self._recorder.record_failure(
                run_id,
                error=str(wrapped.original),
                usage=wrapped.usage,
                steps=wrapped.steps,
                metered_usage=wrapped.metered_usage,
                model_name=scope.model_name,
            )
            await self._notify_run_failed(
                run_id=run_id,
                scope=scope,
                inbound_email=inbound_email,
                exception=wrapped.original,
            )
            raise wrapped.original from None
        except Exception as exc:
            await self._recorder.record_failure(run_id, error=str(exc))
            await self._notify_run_failed(
                run_id=run_id,
                scope=scope,
                inbound_email=inbound_email,
                exception=exc,
            )
            raise

        if agent_result.body.strip() == "QUIETLY_EXIT":
            await self._recorder.record_quiet_exit(
                run_id=run_id,
                scope=scope,
                steps=agent_result.steps,
                usage=agent_result.usage,
                metered_usage=agent_result.metered_usage,
                message_history=agent_result.message_history,
            )
            await self._record_scheduled_agent_quiet_exit(run_id)
            return QuietExited(run_id=run_id)

        # Anything that raises between here and record_completion (attachment
        # read-out, markdown rendering, mailgun send, recorder write) leaves
        # the run un-recorded and the end user/owner uninformed. Treat those
        # the same as a mid-run model failure: persist the partial usage we
        # already have, fire the two failure notifications, then re-raise so
        # procrastinate sees the underlying error.
        try:
            attachment_models = await _read_attachments_out(
                workspace,
                deps.pending_attachments,
            )

            # Owner-as-sender → cc the end-user so they stay in the loop on
            # admin/maintenance threads. End-user-as-sender → no cc. If the
            # owner *is* the end-user (single-tenant personal assistant),
            # there's nobody to cc.
            cc_emails: list[str] = []
            if (
                scope.owner_email
                and scope.end_user_email
                and inbound_email.from_email.lower() == scope.owner_email.lower()
                and scope.owner_email.lower() != scope.end_user_email.lower()
            ):
                cc_emails = [scope.end_user_email]

            envelope = self._envelope_builder.build(
                inbound=inbound_email,
                from_email=scope.inbound_address,
                body_text=agent_result.body,
                attachments=attachment_models,
                message_id_factory=self._message_id_factory,
                run_footer=RunFooterContext(
                    usage=agent_result.usage,
                    run_id=run_id,
                    admin_base_url=self._admin_base_url,
                ),
                cc_emails=cc_emails,
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
                    metered_usage=agent_result.metered_usage,
                    message_history=agent_result.message_history,
                )
            )
            await self._record_scheduled_visible_notification(run_id, scope)
        except Exception as exc:
            await self._recorder.record_failure(
                run_id,
                error=str(exc),
                usage=agent_result.usage,
                steps=agent_result.steps,
                metered_usage=agent_result.metered_usage,
                model_name=scope.model_name,
            )
            await self._notify_run_failed(
                run_id=run_id,
                scope=scope,
                inbound_email=inbound_email,
                exception=exc,
            )
            raise

        return Completed(run_id=run_id, sent=sent)

    async def record_unhandled_run_failure(self, run_id: str, exception: BaseException) -> None:
        """Best-effort fallback for failures that escape `execute_run` before it records them.

        The Procrastinate task wrapper calls this when the job body raises. If
        `execute_run` already recorded the failure, this is a no-op to avoid
        duplicate failure emails and duplicate ledger rows.
        """
        try:
            scope, run, inbound, *_ = await self._load_run(run_id)
        except Exception:
            _log.exception("failed to load run %s while recording unhandled failure", run_id)
            return

        if run.status not in {"queued", "running"}:
            return

        await self._recorder.record_failure(run_id, error=str(exception))
        if self._email_provider is not None:
            await self._notify_run_failed(
                run_id=run_id,
                scope=scope,
                inbound_email=_inbound_email_from_message(inbound),
                exception=exception,
            )

    def _assistant_surface_base_url(self, assistant_id: str) -> str:
        template = self._assistant_surface_base_url_template
        if template is None and self._admin_base_url is not None:
            template = self._admin_base_url.rstrip("/") + "/surfaces/{assistant_id}"
        if template is None:
            return f"/surfaces/{assistant_id}"
        return template.format(assistant_id=assistant_id)

    async def _persist_memory_recalls(self, run_id: str, memories: list[Memory]) -> None:
        """Snapshot what `MemoryPort.recall` returned for this run so the
        admin trace view can show the agent's actual context window. We
        store this rather than re-running recall later because durable
        memory grows between calls — re-running would produce a different
        answer than the agent saw."""
        if not memories:
            return
        async with self._session_factory() as session:
            for m in memories:
                session.add(
                    RunMemoryRecall(
                        id=f"rmr-{uuid.uuid4().hex[:8]}",
                        run_id=run_id,
                        memory_id=m.id,
                        content=m.content,
                        score=m.score,
                    )
                )
            await session.commit()

    async def _load_run(
        self, run_id: str
    ) -> tuple[
        AssistantScope,
        AgentRun,
        EmailMessage,
        EmailThread,
        list[EmailThread],
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
            # Pull every thread + every message for this assistant so the
            # projector can lay out the full conversation history. Heavy
            # assistants may grow this set; if it becomes a problem we'll
            # cap by recency, but for now the agent benefits from full
            # cross-thread context.
            threads = (
                (
                    await session.execute(
                        select(EmailThread)
                        .where(EmailThread.assistant_id == run.assistant_id)
                        .order_by(EmailThread.updated_at.desc(), EmailThread.id)
                    )
                )
                .scalars()
                .all()
            )
            messages = (
                (
                    await session.execute(
                        select(EmailMessage)
                        .where(EmailMessage.assistant_id == run.assistant_id)
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
            return (
                scope,
                run,
                inbound,
                thread,
                list(threads),
                list(messages),
                list(attachments),
            )

    async def _load_prior_thread_history(
        self,
        *,
        assistant_id: str,
        thread_id: str,
        current_run_id: str,
    ) -> list[ModelMessage] | None:
        """Most recent prior `completed`/`quiet_exited` run's Pydantic AI
        message history for this assistant+thread, deserialized so the next
        `agent.run` can resume against full tool-call context. Returns None
        when there is no prior history to thread through.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AgentRun.message_history)
                    .where(
                        AgentRun.assistant_id == assistant_id,
                        AgentRun.thread_id == thread_id,
                        AgentRun.id != current_run_id,
                        AgentRun.status.in_(("completed", "quiet_exited")),
                        AgentRun.message_history.is_not(None),
                    )
                    .order_by(AgentRun.completed_at.desc(), AgentRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return deserialize_message_history(row)

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

    async def _notify_run_failed(
        self,
        *,
        run_id: str,
        scope: AssistantScope,
        inbound_email: NormalizedInboundEmail,
        exception: BaseException,
    ) -> None:
        """Send two best-effort failure notifications: an apologetic, threaded
        note to the end user and a technical, unthreaded note to the owner.

        Both sends are independently guarded — if the end-user send raises,
        the owner send is still attempted; if either raises, the exception
        is logged and swallowed so the original failure surfaces from
        execute_run unchanged.
        """
        assert self._email_provider is not None
        owner_email = await self._lookup_owner_email(scope.owner_id)

        end_user_envelope = build_end_user_error_envelope(
            inbound=inbound_email,
            from_email=scope.inbound_address,
            run_id=run_id,
            message_id_factory=self._message_id_factory,
        )
        try:
            await self._email_provider.send_reply(end_user_envelope)
        except Exception:
            _log.exception("failed to send end-user error notification for run %s", run_id)

        if owner_email:
            owner_envelope = build_owner_error_envelope(
                owner_email=owner_email,
                from_email=scope.inbound_address,
                run_id=run_id,
                exception=exception,
                admin_base_url=self._admin_base_url,
                message_id_factory=self._message_id_factory,
            )
            try:
                await self._email_provider.send_reply(owner_envelope)
            except Exception:
                _log.exception("failed to send owner error notification for run %s", run_id)
        else:
            _log.warning(
                "owner %s has no email configured; skipping owner failure notification for run %s",
                scope.owner_id,
                run_id,
            )

    async def _lookup_owner_email(self, owner_id: str) -> str | None:
        async with self._session_factory() as session:
            owner = await session.get(Owner, owner_id)
            if owner is None:
                return None
            return owner.email or None

    async def _reset_scheduled_unanswered_counters(self, assistant_id: str) -> None:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ScheduledTaskRow).where(
                        ScheduledTaskRow.assistant_id == assistant_id,
                        ScheduledTaskRow.consecutive_unanswered_runs != 0,
                    )
                )
            ).scalars()
            changed = False
            for row in rows:
                row.consecutive_unanswered_runs = 0
                changed = True
            if changed:
                await session.commit()

    async def _record_scheduled_visible_notification(
        self,
        run_id: str,
        scope: AssistantScope,
    ) -> None:
        pause_notice: tuple[str, str] | None = None
        async with self._session_factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None or run.triggered_by_scheduled_task_id is None:
                return
            task = await session.get(ScheduledTaskRow, run.triggered_by_scheduled_task_id)
            if task is None:
                return

            task.consecutive_unanswered_runs += 1
            if (
                task.kind == ScheduledTaskKind.CRON.value
                and task.max_unanswered_runs is not None
                and task.max_unanswered_runs > 0
                and task.consecutive_unanswered_runs >= task.max_unanswered_runs
            ):
                task.status = ScheduledTaskStatus.PAUSED.value
                task.paused_reason = (
                    f"Paused after {task.consecutive_unanswered_runs} scheduled notifications "
                    "with no replies."
                )
                session.add(
                    ScheduledTaskFireRow(
                        id=f"stf-{uuid.uuid4().hex[:10]}",
                        scheduled_task_id=task.id,
                        fired_at=datetime.now(UTC),
                        status="paused",
                        exit_code=None,
                        stdout=None,
                        stderr=task.paused_reason,
                        agent_run_id=run_id,
                    )
                )
                pause_notice = (
                    f"Paused: {task.name}",
                    f"Paused recurring scheduled task '{task.name}' after "
                    f"{task.consecutive_unanswered_runs} notifications with no replies.",
                )
            await session.commit()

        if pause_notice is not None:
            subject, body_text = pause_notice
            try:
                await self.send_scheduled_direct_email(
                    assistant_id=scope.assistant_id,
                    to_email=scope.end_user_email,
                    subject=subject,
                    body_text=body_text,
                )
            except Exception:
                _log.exception("failed to send scheduled task pause notice for run %s", run_id)

    async def _record_scheduled_agent_quiet_exit(self, run_id: str) -> None:
        async with self._session_factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None or run.triggered_by_scheduled_task_id is None:
                return
            session.add(
                ScheduledTaskFireRow(
                    id=f"stf-{uuid.uuid4().hex[:10]}",
                    scheduled_task_id=run.triggered_by_scheduled_task_id,
                    fired_at=datetime.now(UTC),
                    status="agent_quiet_exited",
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    agent_run_id=run_id,
                )
            )
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


async def _project_workspace_emails(
    workspace: AssistantWorkspace,
    projection: ProjectionResult,
) -> None:
    emails_root = projection.run_inputs_dir / "emails"
    if emails_root.exists() and await workspace.project_email_directory(emails_root):
        return
    await workspace.project_emails(_read_projection_files(projection.run_inputs_dir))


async def _read_attachments_out(
    workspace: AssistantWorkspace,
    pending: list[PendingAttachment],
) -> list[EmailAttachment]:
    out: list[EmailAttachment] = []
    for att in pending:
        data = await workspace.read_outbound_attachment(att.sandbox_path)
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
