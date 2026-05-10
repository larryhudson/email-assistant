import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.models import (
    EmailAttachmentRow,
    EmailMessage,
    EmailThread,
    MessageIndex,
)
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import EmailAttachment, NormalizedInboundEmail


@dataclass(frozen=True)
class PersistedInbound:
    """Result of `persist_inbound`.

    `created` is False when the inbound was a duplicate webhook delivery and
    the existing row was returned unchanged; this is what makes the webhook
    fast path idempotent on `(assistant_id, provider_message_id)`.
    """

    message: EmailMessage
    created: bool


async def persist_inbound(
    session: AsyncSession,
    *,
    email: NormalizedInboundEmail,
    scope: AssistantScope,
    thread: EmailThread,
    attachments_root: Path,
) -> PersistedInbound:
    """Persist an inbound email + its attachments + a `message_index` entry.

    Idempotent on `(assistant_id, provider_message_id)`; duplicate Mailgun
    deliveries return the existing row with `created=False`. Caller owns
    the transaction (commits on success).
    """
    existing = await _find_existing(session, scope.assistant_id, email.provider_message_id)
    if existing is not None:
        return PersistedInbound(message=existing, created=False)

    message = EmailMessage(
        id=f"m-{uuid.uuid4().hex[:12]}",
        thread_id=thread.id,
        assistant_id=scope.assistant_id,
        direction="inbound",
        provider_message_id=email.provider_message_id,
        message_id_header=email.message_id_header,
        in_reply_to_header=email.in_reply_to_header,
        references_headers=list(email.references_headers),
        from_email=email.from_email,
        to_emails=list(email.to_emails),
        subject=email.subject,
        body_text=email.body_text,
        body_html=email.body_html,
    )
    session.add(message)
    await session.flush()

    for attachment in email.attachments:
        _write_attachment_row(session, message.id, attachment, attachments_root)

    session.add(
        MessageIndex(
            assistant_id=scope.assistant_id,
            message_id_header=email.message_id_header,
            thread_id=thread.id,
            provider_message_id=email.provider_message_id,
        )
    )
    await session.flush()
    return PersistedInbound(message=message, created=True)


async def _find_existing(
    session: AsyncSession, assistant_id: str, provider_message_id: str
) -> EmailMessage | None:
    stmt = select(EmailMessage).where(
        EmailMessage.assistant_id == assistant_id,
        EmailMessage.provider_message_id == provider_message_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _write_attachment_row(
    session: AsyncSession,
    message_id: str,
    attachment: EmailAttachment,
    attachments_root: Path,
) -> None:
    target_dir = attachments_root / message_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / attachment.filename
    target_path.write_bytes(attachment.data)
    session.add(
        EmailAttachmentRow(
            id=f"att-{uuid.uuid4().hex[:12]}",
            message_id=message_id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            size_bytes=attachment.size_bytes,
            storage_path=str(target_path),
        )
    )
