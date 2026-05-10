import hashlib
import hmac

from email_agent.models.email import (
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
        raise NotImplementedError

    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail:
        raise NotImplementedError("send_reply lands in slice 3")
