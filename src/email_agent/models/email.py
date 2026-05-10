from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EmailAttachment(_Frozen):
    filename: str
    content_type: str
    size_bytes: int
    data: bytes


class NormalizedInboundEmail(_Frozen):
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
    from_email: str
    to_emails: list[str]
    subject: str
    body_text: str
    message_id_header: str
    in_reply_to_header: str | None = None
    references_headers: list[str] = Field(default_factory=list)
    attachments: list[EmailAttachment] = Field(default_factory=list)


class SentEmail(_Frozen):
    provider_message_id: str
    message_id_header: str


class WebhookRequest(_Frozen):
    headers: dict[str, str]
    body: bytes
    form: dict[str, str] = Field(default_factory=dict)
