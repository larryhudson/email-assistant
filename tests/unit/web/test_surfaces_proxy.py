import base64
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    AssistantSurfaceRow,
    Budget,
    EmailMessage,
    EndUser,
    Owner,
)
from email_agent.mail.mailgun import MailgunEmailProvider
from email_agent.runtime.assistant_runtime import AssistantRuntime
from email_agent.web.app import build_app
from email_agent.web.surfaces import SurfaceProxySettings, make_template_surface_target


def _basic_auth(username: str = "larry", password: str = "secret") -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


async def _enable_surface(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assistant_id: str = "a-1",
    port: int = 8000,
) -> None:
    async with session_factory() as session:
        session.add(AssistantSurfaceRow(assistant_id=assistant_id, enabled=True, port=port))
        await session.commit()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    runtime: AssistantRuntime | None = None,
    target_resolver=make_template_surface_target("http://surface.test:{port}"),
):
    return build_app(
        provider=MailgunEmailProvider(signing_key="test-key"),
        runtime=runtime or AssistantRuntime(session_factory, attachments_root=tmp_path),
        session_factory=session_factory,
        admin_basic_auth_username="larry",
        admin_basic_auth_password="secret",
        admin_auth_required=True,
        surface_target_resolver=target_resolver,
    )


async def _seed_assistant(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add(Owner(id="o-1", name="Larry", email="larry@example.com"))
        await session.flush()
        session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
        await session.flush()
        session.add(
            Assistant(
                id="a-1",
                end_user_id="u-1",
                inbound_address="mum@assistants.example.com",
                status="active",
                allowed_senders=["mum@example.com"],
                model="deepseek-flash",
            )
        )
        await session.flush()
        session.add(
            Budget(
                id="b-1",
                assistant_id="a-1",
                monthly_limit_usd=Decimal("10.00"),
                period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
                period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.flush()
        session.add(
            AssistantScopeRow(
                assistant_id="a-1",
                memory_namespace="mum",
                tool_allowlist=[],
                budget_id="b-1",
            )
        )
        await session.commit()


async def test_surface_requires_admin_basic_auth(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _enable_surface(sqlite_session_factory)
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app) as client:
        response = client.get("/surfaces/a-1/")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="email-assistant admin"'


async def test_surface_404s_when_not_enabled(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app) as client:
        response = client.get(
            "/surfaces/a-1/",
            headers={"Authorization": _basic_auth()},
        )

    assert response.status_code == 404


async def test_surface_503s_when_target_is_not_configured(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _enable_surface(sqlite_session_factory)
    app = _build_app(sqlite_session_factory, tmp_path, target_resolver=None)

    with TestClient(app) as client:
        response = client.get(
            "/surfaces/a-1/",
            headers={"Authorization": _basic_auth()},
        )

    assert response.status_code == 503
    assert response.text == "Assistant surface target is not configured\n"


async def test_surface_proxies_to_configured_port_and_path(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_surface(sqlite_session_factory, port=8123)
    seen: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            seen["timeout"] = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            content: bytes,
            headers: dict[str, str],
        ) -> httpx.Response:
            seen.update(
                {
                    "method": method,
                    "url": url,
                    "content": content,
                    "headers": headers,
                }
            )
            return httpx.Response(
                201,
                content=b"created",
                headers={"content-type": "text/plain", "connection": "close"},
            )

    monkeypatch.setattr("email_agent.web.surfaces.httpx.AsyncClient", FakeAsyncClient)
    app = _build_app(
        sqlite_session_factory,
        tmp_path,
        target_resolver=lambda settings: f"http://surface.test:{settings.port}",
    )

    with TestClient(app) as client:
        response = client.post(
            "/surfaces/a-1/api/capture?x=1",
            content=b'{"ok":true}',
            headers={
                "Authorization": _basic_auth(),
                "Cookie": "session=unsafe",
                "X-Assistant-Id": "spoof",
                "X-Viewer-Email": "spoof@example.com",
                "X-Surface-Auth": "spoof",
                "X-Request-Id": "req-1",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 201
    assert response.text == "created"
    assert response.headers["content-type"] == "text/plain"
    assert "connection" not in response.headers
    assert seen["method"] == "POST"
    assert seen["url"] == "http://surface.test:8123/api/capture?x=1"
    assert seen["content"] == b'{"ok":true}'
    seen_headers = cast(dict[str, str], seen["headers"])
    forwarded = {k.lower(): v for k, v in seen_headers.items()}
    assert forwarded["content-type"] == "application/json"
    assert forwarded["x-assistant-id"] == "a-1"
    assert forwarded["x-surface-auth"] == "owner_basic"
    assert forwarded["x-surface-request-id"] == "req-1"
    assert "authorization" not in forwarded
    assert "cookie" not in forwarded
    assert "x-viewer-email" not in forwarded


def test_template_surface_target_uses_assistant_id_and_port() -> None:
    resolver = make_template_surface_target("http://{assistant_id}.surface.local:{port}")

    assert (
        resolver(SurfaceProxySettings(assistant_id="a-1", port=8000))
        == "http://a-1.surface.local:8000"
    )


async def test_surface_action_run_creates_synthetic_inbound_and_queues_run(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    await _enable_surface(sqlite_session_factory)
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
            "/surfaces/a-1/_action/run",
            json={
                "idempotency_key": "expense-123",
                "subject": "Capture expense",
                "body_text": "Pret, 14.50",
            },
            headers={"Authorization": _basic_auth()},
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
    assert message.provider_message_id == "expense-123"
    assert message.from_email == "larry@example.com"
    assert message.to_emails == ["mum@assistants.example.com"]
    assert message.subject == "Capture expense"
    assert message.body_text == "Pret, 14.50"


async def test_surface_action_run_rejects_missing_content_type(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    await _enable_surface(sqlite_session_factory)
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/surfaces/a-1/_action/run",
            content=b'{"body_text":"hello"}',
            headers={"Authorization": _basic_auth()},
        )

    assert response.status_code == 415


async def test_surface_action_run_rejects_non_json_content_type(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    await _enable_surface(sqlite_session_factory)
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/surfaces/a-1/_action/run",
            content="body_text=hello",
            headers={
                "Authorization": _basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    assert response.status_code == 415


async def test_surface_action_run_rejects_cross_origin_origin(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    await _enable_surface(sqlite_session_factory)
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app, base_url="https://agent.example.com") as client:
        response = client.post(
            "/surfaces/a-1/_action/run",
            json={"body_text": "hello"},
            headers={
                "Authorization": _basic_auth(),
                "Origin": "https://evil.example.com",
            },
        )

    assert response.status_code == 403


async def test_surface_action_run_rejects_cross_site_fetch_metadata(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    await _enable_surface(sqlite_session_factory)
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/surfaces/a-1/_action/run",
            json={"body_text": "hello"},
            headers={
                "Authorization": _basic_auth(),
                "Sec-Fetch-Site": "cross-site",
            },
        )

    assert response.status_code == 403


async def test_surface_action_run_allows_same_origin_origin(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    await _enable_surface(sqlite_session_factory)
    app = _build_app(sqlite_session_factory, tmp_path)

    with TestClient(app, base_url="https://agent.example.com") as client:
        response = client.post(
            "/surfaces/a-1/_action/run",
            json={"body_text": "hello"},
            headers={
                "Authorization": _basic_auth(),
                "Origin": "https://agent.example.com",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


async def test_surface_action_run_is_idempotent_on_idempotency_key(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_assistant(sqlite_session_factory)
    await _enable_surface(sqlite_session_factory)
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
        first = client.post(
            "/surfaces/a-1/_action/run",
            json={"idempotency_key": "shortcut-abc", "body_text": "first"},
            headers={"Authorization": _basic_auth()},
        )
        second = client.post(
            "/surfaces/a-1/_action/run",
            json={"idempotency_key": "shortcut-abc", "body_text": "second"},
            headers={"Authorization": _basic_auth()},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(deferred) == 1

    async with sqlite_session_factory() as session:
        messages = (
            (
                await session.execute(
                    select(EmailMessage).where(EmailMessage.provider_message_id == "shortcut-abc")
                )
            )
            .scalars()
            .all()
        )
        runs = (await session.execute(select(AgentRun))).scalars().all()

    assert len(messages) == 1
    assert messages[0].body_text == "first"
    assert len(runs) == 1
