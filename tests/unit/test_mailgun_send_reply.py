import base64
from email.parser import BytesParser
from email.policy import default as default_policy

import httpx
import pytest

from email_agent.mail.mailgun import MailgunEmailProvider
from email_agent.models.email import EmailAttachment, NormalizedOutboundEmail


def _envelope(**overrides) -> NormalizedOutboundEmail:
    return NormalizedOutboundEmail(
        from_email=overrides.pop("from_email", "mum@assistants.example.com"),
        to_emails=overrides.pop("to_emails", ["mum@example.com"]),
        subject=overrides.pop("subject", "Re: Question?"),
        body_text=overrides.pop("body_text", "Sorry, the assistant is at its monthly cap."),
        message_id_header=overrides.pop("message_id_header", "<run-abc@assistants.example.com>"),
        in_reply_to_header=overrides.pop("in_reply_to_header", "<m1@x>"),
        references_headers=overrides.pop("references_headers", ["<r0@x>", "<m1@x>"]),
        attachments=overrides.pop("attachments", []),
    )


def _request_fields(request: httpx.Request) -> tuple[dict[str, list[str]], list[dict]]:
    """Decode a request body (multipart or form-urlencoded) into fields + file parts."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qs

        parsed = parse_qs(request.content.decode(), keep_blank_values=True)
        return parsed, []

    headers = b"".join(f"{name}: {value}\r\n".encode() for name, value in request.headers.items())
    raw = headers + b"\r\n" + request.content
    msg = BytesParser(policy=default_policy).parsebytes(raw)
    fields: dict[str, list[str]] = {}
    files: list[dict] = []
    for part in msg.iter_parts():
        disp = part.get_content_disposition()
        params = dict(part.get_params(header="content-disposition") or [])
        name = params.get("name", "")
        if disp == "form-data" and "filename" in params:
            files.append(
                {
                    "name": name,
                    "filename": params["filename"],
                    "content_type": part.get_content_type(),
                    "data": part.get_payload(decode=True),
                }
            )
        else:
            fields.setdefault(name, []).append(part.get_content())
    return fields, files


@pytest.fixture
def mock_transport_factory():
    captured: list[httpx.Request] = []

    def make(response: httpx.Response) -> tuple[httpx.MockTransport, list[httpx.Request]]:
        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return response

        return httpx.MockTransport(handler), captured

    return make


async def test_send_reply_posts_to_mailgun_messages_endpoint(mock_transport_factory):
    transport, captured = mock_transport_factory(
        httpx.Response(200, json={"id": "<provider-msg-1@mg.example.com>", "message": "Queued"})
    )
    provider = MailgunEmailProvider(
        signing_key="sig",
        api_key="key-123",
        domain="mg.example.com",
        transport=transport,
    )

    sent = await provider.send_reply(_envelope())

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.mailgun.net/v3/mg.example.com/messages"

    expected_auth = "Basic " + base64.b64encode(b"api:key-123").decode()
    assert request.headers["authorization"] == expected_auth

    fields, files = _request_fields(request)
    assert fields["from"] == ["mum@assistants.example.com"]
    assert fields["to"] == ["mum@example.com"]
    assert fields["subject"] == ["Re: Question?"]
    assert fields["text"] == ["Sorry, the assistant is at its monthly cap."]
    assert fields["h:Message-Id"] == ["run-abc@assistants.example.com"]
    assert fields["h:In-Reply-To"] == ["<m1@x>"]
    assert fields["h:References"] == ["<r0@x> <m1@x>"]
    assert files == []

    assert sent.provider_message_id == "<provider-msg-1@mg.example.com>"
    assert sent.message_id_header == "<run-abc@assistants.example.com>"


async def test_send_reply_omits_threading_headers_when_absent(mock_transport_factory):
    transport, captured = mock_transport_factory(
        httpx.Response(200, json={"id": "<x@y>", "message": "Queued"})
    )
    provider = MailgunEmailProvider(
        signing_key="sig",
        api_key="key-123",
        domain="mg.example.com",
        transport=transport,
    )

    await provider.send_reply(_envelope(in_reply_to_header=None, references_headers=[]))

    fields, _ = _request_fields(captured[0])
    assert "h:In-Reply-To" not in fields
    assert "h:References" not in fields


async def _deferred_attachments_test(mock_transport_factory):
    transport, captured = mock_transport_factory(
        httpx.Response(200, json={"id": "<x@y>", "message": "Queued"})
    )
    provider = MailgunEmailProvider(
        signing_key="sig",
        api_key="key-123",
        domain="mg.example.com",
        transport=transport,
    )

    await provider.send_reply(
        _envelope(
            attachments=[
                EmailAttachment(
                    filename="report.pdf",
                    content_type="application/pdf",
                    size_bytes=7,
                    data=b"%PDF-1.7",
                )
            ]
        )
    )

    _, files = _request_fields(captured[0])
    assert len(files) == 1
    f = files[0]
    assert f["name"] == "attachment"
    assert f["filename"] == "report.pdf"
    assert f["content_type"] == "application/pdf"
    assert f["data"] == b"%PDF-1.7"


async def _deferred_error_test(mock_transport_factory):
    from email_agent.mail.mailgun import MailgunSendError

    transport, _ = mock_transport_factory(httpx.Response(401, json={"message": "Forbidden"}))
    provider = MailgunEmailProvider(
        signing_key="sig",
        api_key="bad",
        domain="mg.example.com",
        transport=transport,
    )

    with pytest.raises(MailgunSendError) as excinfo:
        await provider.send_reply(_envelope())

    assert excinfo.value.status_code == 401
    assert "Forbidden" in str(excinfo.value)
