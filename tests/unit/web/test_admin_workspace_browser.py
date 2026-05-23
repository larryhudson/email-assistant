from __future__ import annotations

import base64
from datetime import UTC, datetime
from decimal import Decimal

import pytest
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
)
from email_agent.mail.mailgun import MailgunEmailProvider
from email_agent.runtime.assistant_runtime import AssistantRuntime
from email_agent.sandbox.workspace_provider import InMemoryWorkspaceProvider
from email_agent.web.app import build_app

pytestmark = pytest.mark.xfail(
    strict=True,
    reason="admin workspace browser routes and read-only UI are not implemented yet",
)


def _basic_auth(username: str = "admin", password: str = "secret") -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
async def workspace_provider() -> InMemoryWorkspaceProvider:
    provider = InMemoryWorkspaceProvider()
    workspace = await provider.get_workspace("a-1")
    await workspace.environment.write_text("/workspace/CONTEXT.md", "Larry likes terse replies.\n")
    await workspace.environment.write_text(
        "/workspace/notes/today.md",
        "# Today\n\nRemember to confirm the budget before booking.\n",
    )
    await workspace.environment.write_text("/workspace/notes/archive/old.md", "old note\n")
    await workspace.environment.write_bytes("/workspace/output/report.pdf", b"%PDF\x00binary")
    await workspace.environment.write_text(
        "/workspace/output/huge.txt",
        "BEGIN-HUGE-FILE\n" + ("x" * 300_000) + "\nEND-HUGE-FILE\n",
    )
    return provider


@pytest.fixture
def admin_client(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    workspace_provider: InMemoryWorkspaceProvider,
    tmp_path,
) -> TestClient:
    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path,
        workspace_provider=workspace_provider,
    )
    app = build_app(
        provider=MailgunEmailProvider(signing_key="test-key"),
        runtime=runtime,
        session_factory=sqlite_session_factory,
        admin_basic_auth_username="admin",
        admin_basic_auth_password="secret",
        admin_auth_required=True,
    )
    return TestClient(app)


async def _seed_assistant(
    session: AsyncSession,
    *,
    assistant_id: str = "a-1",
) -> None:
    session.add(Owner(id=f"o-{assistant_id}", name="Larry"))
    session.add(
        EndUser(
            id=f"u-{assistant_id}",
            owner_id=f"o-{assistant_id}",
            email="larry@example.com",
        )
    )
    session.add(
        Budget(
            id=f"b-{assistant_id}",
            assistant_id=assistant_id,
            monthly_limit_usd=Decimal("10.00"),
            period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    session.add(
        Assistant(
            id=assistant_id,
            end_user_id=f"u-{assistant_id}",
            inbound_address=f"{assistant_id}@assistants.example.com",
            status="active",
            allowed_senders=["larry@example.com"],
            model="fireworks/test",
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


async def _seed_run(
    session: AsyncSession,
    *,
    assistant_id: str = "a-1",
    run_id: str = "r-1",
) -> None:
    session.add(
        EmailThread(
            id="t-1",
            assistant_id=assistant_id,
            end_user_id=f"u-{assistant_id}",
            root_message_id=f"<m-in-{run_id}@example.com>",
            subject_normalized="workspace",
        )
    )
    session.add(
        EmailMessage(
            id=f"m-in-{run_id}",
            thread_id="t-1",
            assistant_id=assistant_id,
            direction="inbound",
            provider_message_id=f"prov-in-{run_id}",
            message_id_header=f"<m-in-{run_id}@example.com>",
            from_email="larry@example.com",
            to_emails=[f"{assistant_id}@assistants.example.com"],
            subject="workspace?",
            body_text="Can you inspect your workspace?",
            body_html=None,
        )
    )
    session.add(
        AgentRun(
            id=run_id,
            assistant_id=assistant_id,
            thread_id="t-1",
            inbound_message_id=f"m-in-{run_id}",
            reply_message_id=None,
            status="completed",
            started_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 5, 10, 12, 1, tzinfo=UTC),
        )
    )
    await session.commit()


async def test_authenticated_admin_can_list_assistant_workspace_root(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
) -> None:
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    response = admin_client.get("/admin/assistants/a-1/workspace", headers=_basic_auth())

    assert response.status_code == 200
    body = response.text
    assert "Workspace" in body
    assert "/workspace" in body
    assert "CONTEXT.md" in body
    assert "notes" in body
    assert "output" in body


async def test_workspace_browser_shows_nested_directory_with_breadcrumbs(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
) -> None:
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    response = admin_client.get(
        "/admin/assistants/a-1/workspace",
        params={"path": "notes"},
        headers=_basic_auth(),
    )

    assert response.status_code == 200
    body = response.text
    assert "/workspace" in body
    assert "notes" in body
    assert "today.md" in body
    assert "archive" in body
    assert 'href="/admin/assistants/a-1/workspace"' in body


async def test_workspace_browser_renders_text_file_contents(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
) -> None:
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    response = admin_client.get(
        "/admin/assistants/a-1/workspace",
        params={"path": "notes/today.md"},
        headers=_basic_auth(),
    )

    assert response.status_code == 200
    body = response.text
    assert "today.md" in body
    assert "Remember to confirm the budget before booking." in body
    assert "<pre" in body


async def test_workspace_browser_404s_unknown_assistant(
    admin_client: TestClient,
) -> None:
    response = admin_client.get("/admin/assistants/missing/workspace", headers=_basic_auth())

    assert response.status_code == 404
    body = response.text.lower()
    assert "assistant" in body
    assert "missing" in body


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../CONTEXT.md",
        "/etc/passwd",
        "/workspace/../etc/passwd",
    ],
)
async def test_workspace_browser_rejects_paths_outside_workspace(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
    unsafe_path: str,
) -> None:
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    response = admin_client.get(
        "/admin/assistants/a-1/workspace",
        params={"path": unsafe_path},
        headers=_basic_auth(),
    )

    assert response.status_code in {400, 403}
    assert "outside /workspace" in response.text or "Invalid workspace path" in response.text


async def test_workspace_browser_does_not_inline_binary_files(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
) -> None:
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    response = admin_client.get(
        "/admin/assistants/a-1/workspace",
        params={"path": "output/report.pdf"},
        headers=_basic_auth(),
    )

    assert response.status_code == 200
    assert "report.pdf" in response.text
    assert "binary file" in response.text.lower()
    assert "%PDF" not in response.text


async def test_workspace_browser_does_not_inline_large_text_files(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
) -> None:
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    response = admin_client.get(
        "/admin/assistants/a-1/workspace",
        params={"path": "output/huge.txt"},
        headers=_basic_auth(),
    )

    assert response.status_code == 200
    assert "huge.txt" in response.text
    assert "too large" in response.text.lower()
    assert "BEGIN-HUGE-FILE" not in response.text
    assert "END-HUGE-FILE" not in response.text


async def test_run_workspace_route_resolves_to_current_assistant_workspace(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    admin_client: TestClient,
) -> None:
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)
        await _seed_run(session)

    response = admin_client.get("/admin/runs/r-1/workspace", headers=_basic_auth())

    assert response.status_code == 200
    body = response.text
    assert "Current workspace" in body
    assert "a-1" in body
    assert "CONTEXT.md" in body


async def test_run_workspace_route_404s_unknown_run(admin_client: TestClient) -> None:
    response = admin_client.get("/admin/runs/missing/workspace", headers=_basic_auth())

    assert response.status_code == 404
    body = response.text.lower()
    assert "run" in body
    assert "missing" in body
