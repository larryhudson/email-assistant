from datetime import UTC, datetime

import pytest

from email_agent.adapters.inmemory.email_provider import InMemoryEmailProvider
from email_agent.models.email import (
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    WebhookRequest,
)


def _inbound() -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id="mg-1",
        message_id_header="<mg-1@in>",
        from_email="mum@example.com",
        to_emails=["assistant+mum@example.com"],
        subject="hi",
        body_text="hi there",
        received_at=datetime(2026, 5, 10, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_verify_webhook_is_a_noop_by_default():
    p = InMemoryEmailProvider()
    await p.verify_webhook(WebhookRequest(headers={}, body=b""))


@pytest.mark.asyncio
async def test_verify_webhook_can_be_made_to_fail():
    p = InMemoryEmailProvider(verify_should_raise=ValueError("bad sig"))
    with pytest.raises(ValueError, match="bad sig"):
        await p.verify_webhook(WebhookRequest(headers={}, body=b""))


@pytest.mark.asyncio
async def test_parse_inbound_returns_queued_email():
    p = InMemoryEmailProvider()
    pre = _inbound()
    p.queue_inbound(pre)
    got = await p.parse_inbound(WebhookRequest(headers={}, body=b""))
    assert got == pre


@pytest.mark.asyncio
async def test_parse_inbound_raises_when_empty():
    p = InMemoryEmailProvider()
    with pytest.raises(LookupError):
        await p.parse_inbound(WebhookRequest(headers={}, body=b""))


@pytest.mark.asyncio
async def test_send_reply_records_and_returns_id():
    p = InMemoryEmailProvider()
    out = NormalizedOutboundEmail(
        from_email="assistant+mum@example.com",
        to_emails=["mum@example.com"],
        subject="Re: hi",
        body_text="hi back",
        message_id_header="<reply@out>",
        in_reply_to_header="<mg-1@in>",
    )
    sent = await p.send_reply(out)
    assert sent.message_id_header == "<reply@out>"
    assert sent.provider_message_id.startswith("inmem-")
    assert p.sent == [out]
