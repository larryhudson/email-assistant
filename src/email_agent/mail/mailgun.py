import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx

from email_agent.domain.run_footer import strip_footer
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


class MailgunSendError(Exception):
    """Mailgun rejected an outbound message; carries status + body for logs."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"mailgun send failed: {status_code} {body}")
        self.status_code = status_code
        self.body = body


class MailgunEmailProvider:
    """Mailgun adapter for `EmailProvider`.

    `verify_webhook` checks the HMAC-SHA256 signature Mailgun attaches to
    every webhook (`timestamp + token` signed with the signing key).
    `parse_inbound` translates Mailgun's form payload into the wire model.
    `send_reply` POSTs to Mailgun's messages API with threading headers.
    """

    def __init__(
        self,
        *,
        signing_key: str,
        api_key: str | None = None,
        domain: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._signing_key = signing_key.encode()
        self._api_key = api_key
        self._domain = domain
        self._transport = transport

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
            body_text = strip_footer(form["body-plain"])
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
        if self._api_key is None or self._domain is None:
            raise RuntimeError("send_reply requires api_key and domain")

        data: dict[str, str | list[str]] = {
            "from": reply.from_email,
            "to": list(reply.to_emails),
            "subject": reply.subject,
            "text": reply.body_text,
            "h:Message-Id": reply.message_id_header.strip("<>"),
        }
        if reply.body_html is not None:
            data["html"] = reply.body_html
        if reply.in_reply_to_header is not None:
            data["h:In-Reply-To"] = reply.in_reply_to_header
        if reply.references_headers:
            data["h:References"] = " ".join(reply.references_headers)

        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("attachment", (att.filename, att.data, att.content_type)) for att in reply.attachments
        ]

        url = f"https://api.mailgun.net/v3/{self._domain}/messages"
        auth = ("api", self._api_key)

        async with httpx.AsyncClient(transport=self._transport) as client:
            response = await client.post(
                url,
                data=data,
                files=files or None,
                auth=auth,
            )

        if response.status_code >= 400:
            raise MailgunSendError(response.status_code, response.text)

        payload = response.json()
        return SentEmail(
            provider_message_id=payload["id"],
            message_id_header=reply.message_id_header,
        )


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
