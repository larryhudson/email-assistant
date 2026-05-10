import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)


class MailgunSignatureError(Exception):
    """Mailgun webhook failed signature verification — drop the request."""


class MailgunParseError(Exception):
    """Mailgun webhook payload was malformed — drop the request."""


class MailgunEmailProvider:
    """Mailgun adapter for `EmailProvider`.

    `verify_webhook` checks the HMAC-SHA256 signature Mailgun attaches to
    every webhook (`timestamp + token` signed with the signing key).
    `parse_inbound` translates Mailgun's form payload into the wire model.
    `send_reply` is a placeholder until slice 3.
    """

    def __init__(self, *, signing_key: str) -> None:
        self._signing_key = signing_key.encode()

    async def verify_webhook(self, request: WebhookRequest) -> None:
        try:
            timestamp = request.form["timestamp"]
            token = request.form["token"]
            signature = request.form["signature"]
        except KeyError as exc:
            raise MailgunSignatureError(f"missing signing field: {exc.args[0]}") from exc

        expected = hmac.new(
            self._signing_key,
            f"{timestamp}{token}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise MailgunSignatureError("invalid signature")

    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail:
        form = request.form
        try:
            recipient = form["recipient"]
            sender = form["sender"]
            subject = form["subject"]
            body_text = form["body-plain"]
            message_id_header = form["Message-Id"]
            timestamp = form["timestamp"]
        except KeyError as exc:
            raise MailgunParseError(f"missing required field: {exc.args[0]}") from exc

        headers = _parse_headers(form.get("message-headers", "[]"))
        in_reply_to = headers.get("In-Reply-To")
        references = _split_references(headers.get("References", ""))
        attachments = _parse_attachments(form.get("attachments", "[]"))

        return NormalizedInboundEmail(
            provider_message_id=message_id_header.strip("<>"),
            message_id_header=message_id_header,
            in_reply_to_header=in_reply_to,
            references_headers=references,
            from_email=sender,
            to_emails=[recipient],
            subject=subject,
            body_text=body_text,
            body_html=form.get("body-html") or None,
            attachments=attachments,
            received_at=datetime.fromtimestamp(int(timestamp), tz=UTC),
        )

    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail:
        raise NotImplementedError("send_reply lands in slice 3")


def _parse_headers(raw: str) -> dict[str, str]:
    """Convert Mailgun's `message-headers` JSON list into a dict.

    Mailgun sends headers as `[["Name", "value"], ...]`. Last occurrence wins
    so duplicate header names mirror MTA behaviour.
    """
    try:
        pairs = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MailgunParseError(f"bad message-headers JSON: {exc}") from exc
    return dict(pairs)


def _split_references(raw: str) -> list[str]:
    return [token for token in raw.split() if token]


def _parse_attachments(raw: str) -> list[EmailAttachment]:
    try:
        items = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MailgunParseError(f"bad attachments JSON: {exc}") from exc
    if not items:
        return []

    out: list[EmailAttachment] = []
    for item in items:
        try:
            content_b64 = item["content"]
        except KeyError as exc:
            raise MailgunParseError(
                "attachment missing inline `content` field; URL-fetch path not yet implemented"
            ) from exc
        out.append(
            EmailAttachment(
                filename=item["filename"],
                content_type=item.get("content-type", "application/octet-stream"),
                size_bytes=int(item.get("size", 0)),
                data=base64.b64decode(content_b64),
            )
        )
    return out
