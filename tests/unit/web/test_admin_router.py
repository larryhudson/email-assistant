"""Unit tests for the admin router.

Mounts `make_admin_router(...)` on a bare FastAPI app and exercises the
HTML routes against a SQLite-backed session factory. Tests assert on
key strings that should appear in the rendered HTML — fragile by design,
so a template that stops rendering the assistant id (or whatever) fails
loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EmailThread,
    EndUser,
    Owner,
    RunMemoryRecall,
    RunStep,
    UsageLedger,
)
from email_agent.web.admin.router import make_admin_router


async def _seed_assistant(
    session: AsyncSession,
    *,
    assistant_id: str = "a-1",
    inbound: str = "rose@assistants.example.com",
    end_user_email: str = "mum@example.com",
    monthly_limit: Decimal = Decimal("10.00"),
    model: str = "fireworks/test",
) -> None:
    session.add(Owner(id=f"o-{assistant_id}", name="Larry"))
    session.add(EndUser(id=f"u-{assistant_id}", owner_id=f"o-{assistant_id}", email=end_user_email))
    session.add(
        Budget(
            id=f"b-{assistant_id}",
            assistant_id=assistant_id,
            monthly_limit_usd=monthly_limit,
            period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    session.add(
        Assistant(
            id=assistant_id,
            end_user_id=f"u-{assistant_id}",
            inbound_address=inbound,
            status="active",
            allowed_senders=[end_user_email],
            model=model,
            system_prompt="be kind",
        )
    )
    session.add(
        AssistantScopeRow(
            assistant_id=assistant_id,
            memory_namespace=assistant_id,
            tool_allowlist=["read"],
            budget_id=f"b-{assistant_id}",
        )
    )
    await session.commit()


@pytest.fixture
def admin_client(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> TestClient:
    app = FastAPI()
    app.include_router(make_admin_router(sqlite_session_factory), prefix="/admin")
    return TestClient(app)


async def test_assistants_list_shows_seeded_assistant(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    body = resp.text
    assert "a-1" in body
    assert "rose@assistants.example.com" in body
    assert "active" in body
    assert "fireworks/test" in body


async def _seed_thread_and_run(
    session: AsyncSession,
    *,
    assistant_id: str = "a-1",
    thread_id: str = "t-1",
    run_id: str = "r-1",
    status: str = "completed",
    inbound_body: str = "Hi Rose, what's for dinner?",
    outbound_body: str = "Try chicken thighs and courgette.",
) -> None:
    session.add(
        EmailThread(
            id=thread_id,
            assistant_id=assistant_id,
            end_user_id=f"u-{assistant_id}",
            root_message_id=f"<m-in-{run_id}@x>",
            subject_normalized="dinner",
        )
    )
    session.add(
        EmailMessage(
            id=f"m-in-{run_id}",
            thread_id=thread_id,
            assistant_id=assistant_id,
            direction="inbound",
            provider_message_id=f"prov-in-{run_id}",
            message_id_header=f"<m-in-{run_id}@x>",
            from_email="larry@example.com",
            to_emails=["rose@assistants.example.com"],
            subject="dinner?",
            body_text=inbound_body,
            body_html=None,
        )
    )
    reply_id = None
    if status == "completed":
        reply_id = f"m-out-{run_id}"
        session.add(
            EmailMessage(
                id=reply_id,
                thread_id=thread_id,
                assistant_id=assistant_id,
                direction="outbound",
                provider_message_id=f"prov-out-{run_id}",
                message_id_header=f"<m-out-{run_id}@x>",
                from_email="rose@assistants.example.com",
                to_emails=["larry@example.com"],
                subject="Re: dinner?",
                body_text=outbound_body,
                body_html=None,
            )
        )
    started = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    completed = datetime(2026, 5, 10, 12, 0, 30, tzinfo=UTC) if status != "queued" else None
    session.add(
        AgentRun(
            id=run_id,
            assistant_id=assistant_id,
            thread_id=thread_id,
            inbound_message_id=f"m-in-{run_id}",
            reply_message_id=reply_id,
            status=status,
            error="kaboom" if status == "failed" else None,
            started_at=started,
            completed_at=completed,
        )
    )
    if status == "completed":
        session.add(
            RunStep(
                id=f"s-model-{run_id}",
                run_id=run_id,
                kind="model",
                input_summary="prompt-with-recall",
                output_summary="ok-reply",
                cost_usd=Decimal("0.0007"),
            )
        )
        session.add(
            RunStep(
                id=f"s-bash-{run_id}",
                run_id=run_id,
                kind="tool:bash",
                input_summary="ls /workspace",
                output_summary="emails attachments",
                cost_usd=Decimal("0"),
            )
        )
        session.add(
            UsageLedger(
                id=f"u-{run_id}",
                assistant_id=assistant_id,
                run_id=run_id,
                provider="fireworks",
                model="fireworks/test",
                input_tokens=1500,
                output_tokens=120,
                cost_usd=Decimal("0.0007"),
                budget_period="2026-05",
                created_at=started,
            )
        )
        session.add(
            RunMemoryRecall(
                id=f"rmr-1-{run_id}",
                run_id=run_id,
                memory_id="seed-1",
                content="REMEMBERED-FACT-Z: Larry hates rosemary",
                score=None,
                created_at=started,
            )
        )
    await session.commit()


async def test_runs_list_filters_by_status(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        await _seed_thread_and_run(session, run_id="r-ok", status="completed")
        await _seed_thread_and_run(session, run_id="r-bad", thread_id="t-bad", status="failed")
        await _seed_thread_and_run(
            session, run_id="r-pending", thread_id="t-pending", status="queued"
        )

    all_resp = admin_client.get("/admin/runs")
    assert all_resp.status_code == 200
    assert "r-ok" in all_resp.text
    assert "r-bad" in all_resp.text
    assert "r-pending" in all_resp.text

    completed_resp = admin_client.get("/admin/runs?status=completed")
    assert completed_resp.status_code == 200
    assert "r-ok" in completed_resp.text
    assert "r-bad" not in completed_resp.text
    assert "r-pending" not in completed_resp.text


async def test_run_detail_html_shows_full_trace(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        await _seed_thread_and_run(
            session,
            run_id="r-detail",
            inbound_body="Hi Rose, what's for dinner?",
            outbound_body="Try chicken thighs and courgette.",
        )

    resp = admin_client.get("/admin/runs/r-detail")
    assert resp.status_code == 200
    body = resp.text

    # Inbound + outbound bodies present (Jinja2 escapes apostrophes etc,
    # so look for substrings without those).
    assert "for dinner" in body
    assert "chicken thighs and courgette" in body
    # Recalled memory snapshot present.
    assert "REMEMBERED-FACT-Z: Larry hates rosemary" in body
    # Steps shown — kind + both summaries.
    assert "model" in body
    assert "tool:bash" in body
    assert "ls /workspace" in body
    assert "emails attachments" in body
    # Usage shown.
    assert "1500" in body
    assert "120" in body


async def test_run_detail_json_returns_structured_payload(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        await _seed_thread_and_run(session, run_id="r-json")

    resp = admin_client.get("/admin/runs/r-json.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    payload = resp.json()
    assert payload["id"] == "r-json"
    assert payload["assistant_id"] == "a-1"
    assert payload["status"] == "completed"
    assert payload["inbound"]["body_text"] == "Hi Rose, what's for dinner?"
    assert payload["outbound"]["body_text"] == "Try chicken thighs and courgette."
    assert len(payload["steps"]) == 2
    assert {s["kind"] for s in payload["steps"]} == {"model", "tool:bash"}
    assert len(payload["memory_recalls"]) == 1
    assert payload["memory_recalls"][0]["content"].startswith("REMEMBERED-FACT-Z")
    assert payload["usage"]["input_tokens"] == 1500


async def test_run_detail_404s_unknown_run(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    assert admin_client.get("/admin/runs/r-missing").status_code == 404
    assert admin_client.get("/admin/runs/r-missing.json").status_code == 404
