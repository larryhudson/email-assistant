from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EmailAttachment(_Frozen):
    """A single attached file, in or out. Bytes inline — keep it small."""

    filename: str
    content_type: str
    size_bytes: int
    data: bytes


class NormalizedInboundEmail(_Frozen):
    """Provider-agnostic representation of an inbound email.

    Every `EmailProvider.parse_inbound` adapter produces this so the rest of
    the system never has to know about Mailgun/etc. Headers needed for thread
    matching (`message_id_header`, `in_reply_to_header`, `references_headers`)
    are preserved verbatim.
    """

    provider_message_id: str
    message_id_header: str
    in_reply_to_header: str | None = None
    references_headers: list[str] = Field(default_factory=list)
    from_email: str
    to_emails: list[str]
    subject: str
    body_text: str
    body_html: str | None = None
    attachments: list[EmailAttachment] = Field(default_factory=list)
    received_at: datetime


class NormalizedOutboundEmail(_Frozen):
    """Provider-agnostic reply envelope built by `ReplyEnvelopeBuilder`.

    Threading headers (`in_reply_to_header`, `references_headers`) are filled
    so the recipient's mail client groups the reply with the inbound thread.
    """

    from_email: str
    to_emails: list[str]
    subject: str
    body_text: str
    message_id_header: str
    in_reply_to_header: str | None = None
    references_headers: list[str] = Field(default_factory=list)
    attachments: list[EmailAttachment] = Field(default_factory=list)


class SentEmail(_Frozen):
    """Receipt returned by `EmailProvider.send_reply` after the provider accepts the message."""

    provider_message_id: str
    message_id_header: str


class WebhookRequest(_Frozen):
    """Raw webhook payload as received from the email provider.

    Carrier struct passed to `verify_webhook` and `parse_inbound`. Keeps the
    transport (FastAPI / test harness) decoupled from the parser logic.
    """

    headers: dict[str, str]
    body: bytes
    form: dict[str, str] = Field(default_factory=dict)
