import hashlib
import hmac
import json

import pytest

from email_agent.mail.mailgun import (
    MailgunEmailProvider,
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
