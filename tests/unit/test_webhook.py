import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
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

SIGNING_KEY = "test-key"


def _signed_form(*, recipient: str, sender: str) -> dict[str, str]:
    timestamp = "1747900000"
    token = "tok"
    signature = hmac.new(
        SIGNING_KEY.encode(),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    message_id = "<m-real@example.com>"
    return {
        "timestamp": timestamp,
        "token": token,
        "signature": signature,
        "recipient": recipient,
        "sender": sender,
        "from": sender,
        "subject": "real",
        "body-plain": "hello there",
        "Message-Id": message_id,
        "message-headers": json.dumps([["Message-Id", message_id]]),
    }


async def _seed(session: AsyncSession) -> None:
    session.add(Owner(id="o-1", name="L"))
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
            system_prompt="be kind",
        )
    )
    await session.flush()
    session.add(
        Budget(
            id="b-1",
            assistant_id="a-1",
            monthly_limit_cents=1000,
            period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    await session.flush()
    session.add(
        AssistantScopeRow(
            assistant_id="a-1",
            memory_namespace="mum",
            tool_allowlist=["read"],
            budget_id="b-1",
        )
    )
    await session.commit()


async def test_mailgun_webhook_accepts_signed_payload(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        await _seed(session)

    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    app = build_app(provider=provider, runtime=runtime)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/mailgun",
            data=_signed_form(
                recipient="mum@assistants.example.com",
                sender="mum@example.com",
            ),
        )
    assert response.status_code == 200

    async with sqlite_session_factory() as session:
        rows = (await session.execute(select(EmailMessage))).scalars().all()
        assert len(rows) == 1
        assert rows[0].from_email == "mum@example.com"


async def test_mailgun_webhook_returns_200_for_dropped_inbound(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    app = build_app(provider=provider, runtime=runtime)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/mailgun",
            data=_signed_form(
                recipient="who@example.com",
                sender="mum@example.com",
            ),
        )
    assert response.status_code == 200

    async with sqlite_session_factory() as session:
        rows = (await session.execute(select(EmailMessage))).scalars().all()
        assert rows == []


async def test_mailgun_webhook_rejects_bad_signature(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    app = build_app(provider=provider, runtime=runtime)

    form = _signed_form(
        recipient="mum@assistants.example.com",
        sender="mum@example.com",
    )
    form["signature"] = "0" * 64

    with TestClient(app) as client:
        response = client.post("/webhooks/mailgun", data=form)
    assert response.status_code == 401
