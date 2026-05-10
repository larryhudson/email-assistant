from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)


def test_inbound_email_round_trips():
    email = NormalizedInboundEmail(
        provider_message_id="mg-123",
        message_id_header="<abc@mg.example>",
        in_reply_to_header=None,
        references_headers=[],
        from_email="mum@example.com",
        to_emails=["assistant+mum@example.com"],
        subject="Hi",
        body_text="hello",
        body_html=None,
        attachments=[],
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )
    assert email.from_email == "mum@example.com"
    assert email.attachments == []


def test_inbound_email_rejects_unknown_field():
    with pytest.raises(ValidationError):
        NormalizedInboundEmail(
            provider_message_id="x",
            message_id_header="<x>",
            from_email="a@b.com",
            to_emails=["c@d.com"],
            subject="s",
            body_text="b",
            received_at=datetime.now(UTC),
            bogus_field="nope",  # ty: ignore[unknown-argument]
        )


def test_inbound_email_is_immutable():
    email = NormalizedInboundEmail(
        provider_message_id="x",
        message_id_header="<x>",
        from_email="a@b.com",
        to_emails=["c@d.com"],
        subject="s",
        body_text="b",
        received_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        email.subject = "changed"


def test_attachment_holds_bytes():
    a = EmailAttachment(
        filename="a.pdf",
        content_type="application/pdf",
        size_bytes=4,
        data=b"%PDF",
    )
    assert a.data == b"%PDF"


def test_outbound_email_requires_in_reply_to_when_threading():
    out = NormalizedOutboundEmail(
        from_email="assistant+mum@example.com",
        to_emails=["mum@example.com"],
        subject="Re: Hi",
        body_text="hello back",
        message_id_header="<reply@mg.example>",
        in_reply_to_header="<abc@mg.example>",
        references_headers=["<abc@mg.example>"],
        attachments=[],
    )
    assert out.in_reply_to_header == "<abc@mg.example>"


def test_sent_email_records_provider_id():
    sent = SentEmail(provider_message_id="mg-out-1", message_id_header="<reply@mg.example>")
    assert sent.provider_message_id == "mg-out-1"


def test_webhook_request_is_a_carrier():
    req = WebhookRequest(headers={"X-Sig": "..."}, body=b"raw", form={"from": "a@b"})
    assert req.form["from"] == "a@b"
