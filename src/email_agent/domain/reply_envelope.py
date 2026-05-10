from collections.abc import Callable

from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
)


class ReplyEnvelopeBuilder:
    """Single home for the reply-envelope rules.

    Slice-3's `build_budget_limit_reply` and slice-5's agent runtime both go
    through this builder so the `Re:` + `In-Reply-To` + `References` logic
    lives in one place.
    """

    def build(
        self,
        *,
        inbound: NormalizedInboundEmail,
        from_email: str,
        body_text: str,
        attachments: list[EmailAttachment],
        message_id_factory: Callable[[], str],
    ) -> NormalizedOutboundEmail:
        return NormalizedOutboundEmail(
            from_email=from_email,
            to_emails=[inbound.from_email],
            subject=_re_prefixed(inbound.subject),
            body_text=body_text,
            message_id_header=message_id_factory(),
            in_reply_to_header=inbound.message_id_header,
            references_headers=[*inbound.references_headers, inbound.message_id_header],
            attachments=attachments,
        )


def _re_prefixed(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


__all__ = ["ReplyEnvelopeBuilder"]
