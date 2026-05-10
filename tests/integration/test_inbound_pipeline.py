import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from email_agent.config import Settings
from email_agent.db.models import (
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EmailThread,
    EndUser,
    MessageIndex,
    Owner,
)
from email_agent.db.session import make_engine, make_session_factory, session_scope
from email_agent.domain.inbound_persister import persist_inbound
from email_agent.domain.router import AssistantRouter, Routed
from email_agent.domain.thread_resolver import ThreadResolver
from email_agent.mail.mailgun import MailgunEmailProvider
from email_agent.models.email import WebhookRequest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SIGNING_KEY = "test-signing-key"


def _signed_form(*, recipient: str, sender: str) -> dict[str, str]:
    timestamp = "1747900000"
    token = "tok"
    signature = hmac.new(
        SIGNING_KEY.encode(),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    return {
        "timestamp": timestamp,
        "token": token,
        "signature": signature,
        "recipient": recipient,
        "sender": sender,
        "from": sender,
        "subject": "hello there",
        "body-plain": "real body",
        "Message-Id": message_id,
        "message-headers": json.dumps([["Message-Id", message_id]]),
        "attachments": json.dumps(
            [
                {
                    "filename": "note.txt",
                    "content-type": "text/plain",
                    "size": 5,
                    "content": base64.b64encode(b"hello").decode(),
                }
            ]
        ),
    }


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs db")
async def test_round_trip_pipeline_persists_message_index_and_attachments(tmp_path):
    settings = Settings()  # ty: ignore[missing-argument]
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    suffix = uuid.uuid4().hex[:8]
    inbound_address = f"a-{suffix}@assistants.example.com"
    sender = f"mum-{suffix}@example.com"

    async with session_scope(factory) as s:
        s.add(Owner(id=f"o-{suffix}", name="Larry"))
        await s.flush()
        s.add(EndUser(id=f"u-{suffix}", owner_id=f"o-{suffix}", email=sender))
        await s.flush()
        s.add(
            Assistant(
                id=f"a-{suffix}",
                end_user_id=f"u-{suffix}",
                inbound_address=inbound_address,
                status="active",
                allowed_senders=[sender],
                model="deepseek-flash",
                system_prompt="be kind",
            )
        )
        await s.flush()
        s.add(
            Budget(
                id=f"b-{suffix}",
                assistant_id=f"a-{suffix}",
                monthly_limit_usd=Decimal("10.00"),
                period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
                period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await s.flush()
        s.add(
            AssistantScopeRow(
                assistant_id=f"a-{suffix}",
                memory_namespace=f"ns-{suffix}",
                tool_allowlist=["read"],
                budget_id=f"b-{suffix}",
            )
        )

    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    request = WebhookRequest(
        headers={},
        body=b"",
        form=_signed_form(recipient=inbound_address, sender=sender),
    )
    await provider.verify_webhook(request)
    email = await provider.parse_inbound(request)

    router = AssistantRouter(factory)
    outcome = await router.resolve(email)
    assert isinstance(outcome, Routed)

    resolver = ThreadResolver(factory)
    thread = await resolver.resolve(email, outcome.scope)

    async with session_scope(factory) as s:
        thread = await s.get(EmailThread, thread.id)
        assert thread is not None
        result = await persist_inbound(
            s,
            email=email,
            scope=outcome.scope,
            thread=thread,
            attachments_root=tmp_path,
        )
        assert result.created is True

    async with session_scope(factory) as s:
        msg = (
            await s.execute(
                select(EmailMessage).where(
                    EmailMessage.assistant_id == f"a-{suffix}",
                )
            )
        ).scalar_one()
        idx = (
            await s.execute(
                select(MessageIndex).where(
                    MessageIndex.assistant_id == f"a-{suffix}",
                )
            )
        ).scalar_one()
        assert msg.message_id_header == email.message_id_header
        assert idx.thread_id == thread.id

    await engine.dispose()
