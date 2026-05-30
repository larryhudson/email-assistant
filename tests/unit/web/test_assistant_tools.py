from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EndUser,
    Owner,
)
from email_agent.mail.mailgun import MailgunEmailProvider
from email_agent.runtime.assistant_runtime import AssistantRuntime
from email_agent.web.app import build_app


async def _seed_assistant(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add(Owner(id="o-1", name="Larry", email="larry@example.com"))
        session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
        session.add(
            Assistant(
                id="a-1",
                end_user_id="u-1",
                inbound_address="mum@assistants.example.com",
                status="active",
                allowed_senders=["mum@example.com"],
                model="test-model",
            )
        )
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
            AssistantScopeRow(
                assistant_id="a-1",
                memory_namespace="mum",
                tool_allowlist=[],
                budget_id="b-1",
            )
        )
        await session.commit()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    runtime: AssistantRuntime | None = None,
    assistant_tools_token: str | None = None,
):
    return build_app(
        provider=MailgunEmailProvider(signing_key="test-key"),
        runtime=runtime or AssistantRuntime(session_factory, attachments_root=tmp_path),
        session_factory=session_factory,
        assistant_tools_token=assistant_tools_token,
    )


async def test_assistant_tools_openapi_is_published(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app) as client:
        response = client.get("/_internal/assistant-tools/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert spec["openapi"] == "3.1.0"
    assert spec["paths"]["/v1/runs"]["post"]["operationId"] == "runs.create"
    assert spec["paths"]["/v1/runs/{run_id}"]["get"]["operationId"] == "runs.get"
    assert spec["paths"]["/v1/events"]["post"]["operationId"] == "events.log"


async def test_assistant_tools_openapi_remains_public_when_token_configured(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    app = _build_app(sqlite_session_factory, tmp_path, assistant_tools_token="tools-secret")

    with TestClient(app) as client:
        response = client.get("/_internal/assistant-tools/openapi.json")

    assert response.status_code == 200


async def test_assistant_tools_runs_create_queues_normal_agent_run(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    deferred: list[dict[str, str]] = []

    async def defer_run_agent(*, run_id: str, assistant_id: str) -> None:
        deferred.append({"run_id": run_id, "assistant_id": assistant_id})

    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path,
        run_agent_defer=defer_run_agent,
    )
    app = _build_app(sqlite_session_factory, tmp_path, runtime=runtime)

    with TestClient(app) as client:
        response = client.post(
            "/_internal/assistant-tools/v1/runs",
            headers={"X-Assistant-Id": "a-1"},
            json={
                "reason": "surface_capture",
                "input": {"amount": 14.5, "merchant": "Pret"},
                "idempotency_key": "tools-run-1",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert deferred == [{"run_id": payload["run_id"], "assistant_id": "a-1"}]

    async with sqlite_session_factory() as session:
        run = await session.get(AgentRun, payload["run_id"])
        assert run is not None
        message = await session.get(EmailMessage, run.inbound_message_id)

    assert message is not None
    assert message.provider_message_id == "tools-run-1"
    assert message.subject == "Assistant tool: surface_capture"
    assert '"merchant": "Pret"' in message.body_text


async def test_assistant_tools_token_required_when_configured(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    app = _build_app(
        sqlite_session_factory,
        tmp_path,
        assistant_tools_token="tools-secret",
    )

    with TestClient(app) as client:
        missing = client.post(
            "/_internal/assistant-tools/v1/runs",
            headers={"X-Assistant-Id": "a-1"},
            json={"reason": "surface_capture"},
        )
        wrong = client.post(
            "/_internal/assistant-tools/v1/runs",
            headers={
                "Authorization": "Bearer wrong",
                "X-Assistant-Id": "a-1",
            },
            json={"reason": "surface_capture"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 403


async def test_assistant_tools_correct_token_allows_privileged_routes(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    app = _build_app(
        sqlite_session_factory,
        tmp_path,
        runtime=runtime,
        assistant_tools_token="tools-secret",
    )
    auth_headers = {
        "Authorization": "Bearer tools-secret",
        "X-Assistant-Id": "a-1",
    }

    with TestClient(app) as client:
        created = client.post(
            "/_internal/assistant-tools/v1/runs",
            headers=auth_headers,
            json={"reason": "surface_capture", "idempotency_key": "token-run"},
        )
        run_id = created.json()["run_id"]
        fetched = client.get(
            f"/_internal/assistant-tools/v1/runs/{run_id}",
            headers=auth_headers,
        )
        event = client.post(
            "/_internal/assistant-tools/v1/events",
            headers=auth_headers,
            json={"event": "surface_loaded"},
        )

    assert created.status_code == 200
    assert fetched.status_code == 200
    assert event.status_code == 200


async def test_assistant_tools_runs_get_is_scoped_to_assistant(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    await runtime.accept_surface_action(
        assistant_id="a-1",
        subject="Assistant tool: inspect",
        body_text="inspect",
        provider_message_id="tools-run-2",
        message_id_header="<tools-run-2@example.com>",
    )
    async with sqlite_session_factory() as session:
        run_id = (await session.execute(select(AgentRun.id))).scalar_one()

    app = _build_app(sqlite_session_factory, tmp_path, runtime=runtime)

    with TestClient(app) as client:
        response = client.get(
            f"/_internal/assistant-tools/v1/runs/{run_id}",
            headers={"X-Assistant-Id": "a-1"},
        )
        wrong_assistant = client.get(
            f"/_internal/assistant-tools/v1/runs/{run_id}",
            headers={"X-Assistant-Id": "other"},
        )

    assert response.status_code == 200
    assert response.json()["run_id"] == run_id
    assert response.json()["status"] == "queued"
    assert wrong_assistant.status_code == 404


async def test_assistant_tools_events_log_returns_logged(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/_internal/assistant-tools/v1/events",
            headers={"X-Assistant-Id": "a-1"},
            json={"event": "surface_loaded", "detail": {"path": "/"}},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "logged"}
