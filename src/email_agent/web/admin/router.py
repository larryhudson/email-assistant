"""Admin router — read-only views for inspecting assistants and runs.

`make_admin_router(session_factory)` returns an `APIRouter` ready to be
mounted under `/admin` on the main FastAPI app. All routes are server-
rendered HTML against `templates/`. The parent app protects `/admin` with
Basic Auth when built from environment-backed settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select

from email_agent.db.models import (
    AgentRun,
    Assistant,
    Budget,
    EmailMessage,
    RunMemoryRecall,
    RunStep,
    UsageLedger,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
ADMIN_STATIC_DIR = _HERE / "static"

_PAGE_SIZE = 50


@dataclass(frozen=True)
class _AssistantRow:
    id: str
    inbound_address: str
    status: str
    model: str
    monthly_limit: Decimal
    spent: Decimal
    last_run_at: datetime | None


@dataclass(frozen=True)
class _RunRow:
    id: str
    assistant_id: str
    thread_id: str
    status: str
    started_at: datetime | None
    cost: Decimal


class _MessagePayload(BaseModel):
    id: str
    direction: str
    from_email: str
    to_emails: list[str]
    subject: str
    body_text: str | None
    message_id_header: str | None
    in_reply_to_header: str | None


class _StepPayload(BaseModel):
    id: str
    kind: str
    input_summary: str
    output_summary: str
    cost_usd: Decimal


class _MemoryRecallPayload(BaseModel):
    id: str
    memory_id: str | None
    content: str
    score: float | None


class _UsagePayload(BaseModel):
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    budget_period: str


class _RunDetailPayload(BaseModel):
    id: str
    assistant_id: str
    thread_id: str
    status: str
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    system_prompt: str | None
    user_prompt: str | None
    inbound: _MessagePayload | None
    outbound: _MessagePayload | None
    steps: list[_StepPayload]
    memory_recalls: list[_MemoryRecallPayload]
    usage: list[_UsagePayload]
    usage_total_cost: Decimal


def make_admin_router(session_factory: async_sessionmaker[AsyncSession]) -> APIRouter:
    router = APIRouter()
    # Static files are mounted on the parent app, not the router —
    # `router.mount()` doesn't compose with `include_router(prefix=...)`.
    # See `mount_admin` below.

    @router.get("/", response_class=HTMLResponse)
    async def assistants_list(request: Request) -> HTMLResponse:
        rows = await _load_assistants(session_factory)
        return _TEMPLATES.TemplateResponse(request, "assistants.html", {"rows": rows})

    @router.get("/runs", response_class=HTMLResponse)
    async def runs_list(
        request: Request,
        assistant_id: str | None = None,
        status: str | None = None,
    ) -> HTMLResponse:
        runs = await _load_runs(
            session_factory, assistant_id=assistant_id, status=status, limit=_PAGE_SIZE
        )
        assistant_filter_options = await _load_assistant_ids(session_factory)
        return _TEMPLATES.TemplateResponse(
            request,
            "runs_list.html",
            {
                "runs": runs,
                "filter_assistant_id": assistant_id,
                "filter_status": status,
                "assistant_filter_options": assistant_filter_options,
            },
        )

    # Declare the .json route BEFORE the HTML one — FastAPI's path
    # converter is greedy, so `/runs/{run_id}` would otherwise capture
    # `r-foo.json` whole and the JSON route would never match.
    @router.get("/runs/{run_id}.json", response_model=_RunDetailPayload)
    async def run_detail_json(run_id: str) -> _RunDetailPayload:
        detail = await _load_run_detail(session_factory, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return detail

    @router.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: str) -> HTMLResponse:
        detail = await _load_run_detail(session_factory, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return _TEMPLATES.TemplateResponse(
            request,
            "run_detail.html",
            {
                "run": detail,
                "inbound": detail.inbound,
                "outbound": detail.outbound,
                "steps": detail.steps,
                "memory_recalls": detail.memory_recalls,
                "usage": detail.usage,
                "usage_total_cost": detail.usage_total_cost,
            },
        )

    return router


def mount_admin(app, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Mount the admin router + its static files on a FastAPI app at /admin."""
    app.include_router(make_admin_router(session_factory), prefix="/admin")
    app.mount("/admin/static", StaticFiles(directory=str(ADMIN_STATIC_DIR)), name="admin-static")


async def _load_assistants(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[_AssistantRow]:
    async with session_factory() as session:
        # Per-assistant aggregates: sum(usage_ledger.cost_usd) for spent,
        # max(agent_runs.completed_at) for last run. Left-joined so an
        # assistant with zero runs still shows.
        stmt = (
            select(
                Assistant.id,
                Assistant.inbound_address,
                Assistant.status,
                Assistant.model,
                Budget.monthly_limit_usd,
                func.coalesce(func.sum(UsageLedger.cost_usd), 0).label("spent"),
                func.max(AgentRun.completed_at).label("last_run_at"),
            )
            .join(Budget, Budget.assistant_id == Assistant.id)
            .outerjoin(UsageLedger, UsageLedger.assistant_id == Assistant.id)
            .outerjoin(AgentRun, AgentRun.assistant_id == Assistant.id)
            .group_by(
                Assistant.id,
                Assistant.inbound_address,
                Assistant.status,
                Assistant.model,
                Budget.monthly_limit_usd,
            )
            .order_by(Assistant.id)
        )
        result = await session.execute(stmt)
        return [
            _AssistantRow(
                id=row.id,
                inbound_address=row.inbound_address,
                status=row.status,
                model=row.model,
                monthly_limit=row.monthly_limit_usd,
                spent=Decimal(str(row.spent)),
                last_run_at=row.last_run_at,
            )
            for row in result.all()
        ]


async def _load_assistant_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    async with session_factory() as session:
        result = await session.execute(select(Assistant.id).order_by(Assistant.id))
        return [r[0] for r in result.all()]


async def _load_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assistant_id: str | None,
    status: str | None,
    limit: int,
) -> list[_RunRow]:
    async with session_factory() as session:
        stmt = (
            select(
                AgentRun.id,
                AgentRun.assistant_id,
                AgentRun.thread_id,
                AgentRun.status,
                AgentRun.started_at,
                func.coalesce(
                    select(func.sum(UsageLedger.cost_usd))
                    .where(UsageLedger.run_id == AgentRun.id)
                    .scalar_subquery(),
                    0,
                ).label("cost"),
            )
            .order_by(AgentRun.started_at.desc())
            .limit(limit)
        )
        if assistant_id:
            stmt = stmt.where(AgentRun.assistant_id == assistant_id)
        if status:
            stmt = stmt.where(AgentRun.status == status)
        result = await session.execute(stmt)
        return [
            _RunRow(
                id=row.id,
                assistant_id=row.assistant_id,
                thread_id=row.thread_id,
                status=row.status,
                started_at=row.started_at,
                cost=Decimal(str(row.cost)),
            )
            for row in result.all()
        ]


async def _load_run_detail(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> _RunDetailPayload | None:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            return None
        inbound = await session.get(EmailMessage, run.inbound_message_id)
        outbound = (
            await session.get(EmailMessage, run.reply_message_id) if run.reply_message_id else None
        )
        steps = (
            (
                await session.execute(
                    select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.created_at)
                )
            )
            .scalars()
            .all()
        )
        recalls = (
            (
                await session.execute(
                    select(RunMemoryRecall)
                    .where(RunMemoryRecall.run_id == run_id)
                    .order_by(RunMemoryRecall.created_at)
                )
            )
            .scalars()
            .all()
        )
        usage_rows = (
            (
                await session.execute(
                    select(UsageLedger)
                    .where(UsageLedger.run_id == run_id)
                    .order_by(UsageLedger.created_at, UsageLedger.id)
                )
            )
            .scalars()
            .all()
        )

        return _RunDetailPayload(
            id=run.id,
            assistant_id=run.assistant_id,
            thread_id=run.thread_id,
            status=run.status,
            error=run.error,
            started_at=run.started_at,
            completed_at=run.completed_at,
            system_prompt=run.system_prompt,
            user_prompt=run.user_prompt,
            inbound=_message_payload(inbound) if inbound else None,
            outbound=_message_payload(outbound) if outbound else None,
            steps=[
                _StepPayload(
                    id=s.id,
                    kind=s.kind,
                    input_summary=s.input_summary,
                    output_summary=s.output_summary,
                    cost_usd=s.cost_usd,
                )
                for s in steps
            ],
            memory_recalls=[
                _MemoryRecallPayload(
                    id=r.id, memory_id=r.memory_id, content=r.content, score=r.score
                )
                for r in recalls
            ],
            usage=[
                _UsagePayload(
                    provider=usage.provider,
                    model=usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost_usd,
                    budget_period=usage.budget_period,
                )
                for usage in usage_rows
            ],
            usage_total_cost=sum((u.cost_usd for u in usage_rows), Decimal("0")),
        )


def _message_payload(m: EmailMessage) -> _MessagePayload:
    return _MessagePayload(
        id=m.id,
        direction=m.direction,
        from_email=m.from_email,
        to_emails=list(m.to_emails),
        subject=m.subject,
        body_text=m.body_text,
        message_id_header=m.message_id_header,
        in_reply_to_header=m.in_reply_to_header,
    )


__all__ = ["make_admin_router"]
