"""Parse a `.eml` fixture into a `NormalizedInboundEmail`.

Used by the `inject-email` CLI for fixture-driven local development. Not
involved in the production webhook path — that goes through the Mailgun
adapter.
"""

from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from email_agent.models.email import EmailAttachment, NormalizedInboundEmail


def parse_eml_file(path: Path) -> NormalizedInboundEmail:
    """Parse a `.eml` file into a `NormalizedInboundEmail`.

    Multi-recipient `To:` is preserved. Body is the first text/plain part;
    HTML companion (if any) is captured as `body_html`. Attachments are
    inlined as bytes — keep your fixtures small.
    """
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())

    from_email = _first_address(msg.get("From", ""))
    to_emails = _all_addresses(msg.get("To", ""))
    subject = msg.get("Subject", "") or ""
    message_id_header = msg.get("Message-ID", "") or ""
    in_reply_to = msg.get("In-Reply-To") or None
    references = _split_references(msg.get("References", ""))

    received_at = _received_at(msg.get("Date"))

    body_text = ""
    body_html: str | None = None
    attachments: list[EmailAttachment] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        ctype = part.get_content_type()
        if disp == "attachment":
            payload = part.get_payload(decode=True)
            data = payload if isinstance(payload, bytes) else b""
            attachments.append(
                EmailAttachment(
                    filename=part.get_filename() or "attachment",
                    content_type=ctype,
                    size_bytes=len(data),
                    data=data,
                )
            )
            continue
        if ctype == "text/plain" and not body_text:
            body_text = part.get_content().rstrip("\r\n") + "\n"
        elif ctype == "text/html" and body_html is None:
            body_html = part.get_content()

    return NormalizedInboundEmail(
        provider_message_id=message_id_header.strip("<>") or path.stem,
        message_id_header=message_id_header,
        in_reply_to_header=in_reply_to,
        references_headers=references,
        from_email=from_email,
        to_emails=to_emails,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        received_at=received_at,
    )


def _first_address(header_value: str) -> str:
    parsed = getaddresses([header_value])
    return parsed[0][1] if parsed else ""


def _all_addresses(header_value: str) -> list[str]:
    return [addr for _, addr in getaddresses([header_value]) if addr]


def _split_references(raw: str) -> list[str]:
    return [token for token in raw.split() if token]


def _received_at(date_header: str | None) -> datetime:
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


__all__ = ["parse_eml_file"]
