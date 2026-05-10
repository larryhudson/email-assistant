import base64
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.mail.mailgun import MailgunEmailProvider
from email_agent.runtime.assistant_runtime import AssistantRuntime
from email_agent.web.app import build_app


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


async def test_admin_requires_basic_auth(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    app = build_app(
        provider=MailgunEmailProvider(signing_key="test-key"),
        runtime=AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path),
        session_factory=sqlite_session_factory,
        admin_basic_auth_username="larry",
        admin_basic_auth_password="secret",
        admin_auth_required=True,
    )

    with TestClient(app) as client:
        response = client.get("/admin/")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="email-assistant admin"'


async def test_admin_allows_valid_basic_auth(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    app = build_app(
        provider=MailgunEmailProvider(signing_key="test-key"),
        runtime=AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path),
        session_factory=sqlite_session_factory,
        admin_basic_auth_username="larry",
        admin_basic_auth_password="secret",
        admin_auth_required=True,
    )

    with TestClient(app) as client:
        response = client.get(
            "/admin/",
            headers={"Authorization": _basic_auth("larry", "secret")},
        )

    assert response.status_code == 200


async def test_admin_auth_fails_closed_without_credentials(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    app = build_app(
        provider=MailgunEmailProvider(signing_key="test-key"),
        runtime=AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path),
        session_factory=sqlite_session_factory,
        admin_auth_required=True,
    )

    with TestClient(app) as client:
        response = client.get("/admin/")

    assert response.status_code == 503
