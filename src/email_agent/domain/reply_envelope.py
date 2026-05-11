from collections.abc import Callable

from markdown_it import MarkdownIt

from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
)

# commonmark preset disables raw-HTML pass-through, so literal `<script>` etc.
# in the agent's body gets escaped to `&lt;script&gt;` rather than rendered.
_MD = MarkdownIt("commonmark", {"html": False})


def render_markdown_to_html(body_text: str) -> str:
    """Render the agent's markdown body to HTML for a mail client.

    Empty / whitespace-only input renders to an empty string so the caller
    can decide whether to set `body_html` at all.
    """
    if not body_text.strip():
        return ""
    return _MD.render(body_text)


class ReplyEnvelopeBuilder:
    """Single home for the reply-envelope rules.

    Slice-3's `build_budget_limit_reply` and slice-5's agent runtime both go
    through this builder so the `Re:` + `In-Reply-To` + `References` logic
    lives in one place. The agent authors `body_text` as markdown; the
    builder renders an HTML alternative so the recipient's mail client sees
    formatted output.
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
        rendered = render_markdown_to_html(body_text)
        body_html = rendered or None
        return NormalizedOutboundEmail(
            from_email=from_email,
            to_emails=[inbound.from_email],
            subject=_re_prefixed(inbound.subject),
            body_text=body_text,
            body_html=body_html,
            message_id_header=message_id_factory(),
            in_reply_to_header=inbound.message_id_header,
            references_headers=[*inbound.references_headers, inbound.message_id_header],
            attachments=attachments,
        )


def _re_prefixed(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


__all__ = ["ReplyEnvelopeBuilder", "render_markdown_to_html"]
