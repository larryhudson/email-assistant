import base64
import hashlib
import hmac
import json

import pytest

from email_agent.mail.mailgun import (
    MailgunEmailProvider,
    MailgunParseError,
    MailgunSignatureError,
)
from email_agent.models.email import WebhookRequest

SIGNING_KEY = "test-signing-key"
TIMESTAMP = "1747900000"
TOKEN = "abc123"


def _signature(signing_key: str = SIGNING_KEY) -> str:
    return hmac.new(
        signing_key.encode(),
        f"{TIMESTAMP}{TOKEN}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _form(**overrides: str) -> dict[str, str]:
    base = {
        "timestamp": TIMESTAMP,
        "token": TOKEN,
        "signature": _signature(),
        "recipient": "a-1@assistants.example.com",
        "sender": "mum@example.com",
        "from": "Mum <mum@example.com>",
        "subject": "hi",
        "body-plain": "hello",
        "Message-Id": "<m-1@example.com>",
        "message-headers": json.dumps(
            [
                ["Message-Id", "<m-1@example.com>"],
                ["From", "Mum <mum@example.com>"],
                ["To", "a-1@assistants.example.com"],
                ["Subject", "hi"],
            ]
        ),
    }
    base.update(overrides)
    return base


async def test_verify_webhook_accepts_valid_signature():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    await provider.verify_webhook(
        WebhookRequest(headers={}, body=b"", form=_form()),
    )


async def test_verify_webhook_rejects_bad_signature():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    with pytest.raises(MailgunSignatureError):
        await provider.verify_webhook(
            WebhookRequest(headers={}, body=b"", form=_form(signature="0" * 64)),
        )


async def test_verify_webhook_rejects_missing_signature_fields():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    form = _form()
    del form["timestamp"]
    with pytest.raises(MailgunSignatureError):
        await provider.verify_webhook(
            WebhookRequest(headers={}, body=b"", form=form),
        )


async def test_parse_inbound_maps_basic_fields():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    email = await provider.parse_inbound(
        WebhookRequest(headers={}, body=b"", form=_form()),
    )

    assert email.provider_message_id == "m-1@example.com"
    assert email.message_id_header == "<m-1@example.com>"
    assert email.from_email == "mum@example.com"
    assert email.to_emails == ["a-1@assistants.example.com"]
    assert email.subject == "hi"
    assert email.body_text == "hello"
    assert email.body_html is None
    assert email.in_reply_to_header is None
    assert email.references_headers == []


async def test_parse_inbound_extracts_in_reply_to_and_references():
    headers = json.dumps(
        [
            ["Message-Id", "<m-2@example.com>"],
            ["In-Reply-To", "<prev@example.com>"],
            ["References", "<root@example.com> <prev@example.com>"],
        ]
    )
    form = _form(**{"message-headers": headers, "Message-Id": "<m-2@example.com>"})
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    email = await provider.parse_inbound(
        WebhookRequest(headers={}, body=b"", form=form),
    )
    assert email.in_reply_to_header == "<prev@example.com>"
    assert email.references_headers == ["<root@example.com>", "<prev@example.com>"]


async def test_parse_inbound_includes_html_body_when_present():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    email = await provider.parse_inbound(
        WebhookRequest(
            headers={},
            body=b"",
            form=_form(**{"body-html": "<p>hello</p>"}),
        ),
    )
    assert email.body_html == "<p>hello</p>"


async def test_parse_inbound_raises_on_missing_required_field():
    form = _form()
    del form["body-plain"]
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    with pytest.raises(MailgunParseError):
        await provider.parse_inbound(
            WebhookRequest(headers={}, body=b"", form=form),
        )


async def test_parse_inbound_includes_inline_attachments():
    encoded = base64.b64encode(b"%PDF").decode()
    attachments_field = json.dumps(
        [
            {
                "filename": "receipt.pdf",
                "content-type": "application/pdf",
                "size": 4,
                "content": encoded,
            }
        ]
    )
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    email = await provider.parse_inbound(
        WebhookRequest(
            headers={},
            body=b"",
            form=_form(attachments=attachments_field),
        ),
    )

    assert len(email.attachments) == 1
    att = email.attachments[0]
    assert att.filename == "receipt.pdf"
    assert att.content_type == "application/pdf"
    assert att.size_bytes == 4
    assert att.data == b"%PDF"
