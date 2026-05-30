import base64
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import AssistantSurfaceRow
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
    target_resolver=make_template_surface_target("http://surface.test:{port}"),
):
    return build_app(
        provider=MailgunEmailProvider(signing_key="test-key"),
        runtime=AssistantRuntime(session_factory, attachments_root=tmp_path),
        session_factory=session_factory,
        admin_basic_auth_username="larry",
        admin_basic_auth_password="secret",
        admin_auth_required=True,
        surface_target_resolver=target_resolver,
    )


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
