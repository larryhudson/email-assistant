from collections.abc import Callable
from dataclasses import dataclass

from markdown_it import MarkdownIt

from email_agent.domain.run_footer import render_run_footer
from email_agent.models.agent import RunUsage
from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
)

# gfm-like = CommonMark + tables + strikethrough + autolinks (bare URLs
# become clickable) + task lists. Matches what LLMs reach for by default,
# and what mail clients are happy to render. `html=False` keeps raw HTML
# in the agent body escaped (no <script> injection from a chatty model).
_MD = MarkdownIt("gfm-like", {"html": False})


def render_markdown_to_html(body_text: str) -> str:
    """Render the agent's markdown body to HTML for a mail client.

    Empty / whitespace-only input renders to an empty string so the caller
    can decide whether to set `body_html` at all.
    """
    if not body_text.strip():
        return ""
    return _MD.render(body_text)


@dataclass(frozen=True)
class RunFooterContext:
    """Inputs needed to render the cost/run footer on outbound replies."""

    usage: RunUsage
    run_id: str
    admin_base_url: str | None


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
        run_footer: RunFooterContext | None = None,
    ) -> NormalizedOutboundEmail:
        rendered = render_markdown_to_html(body_text)
        final_text = body_text
        final_html = rendered or None

        if run_footer is not None:
            footer = render_run_footer(
                run_footer.usage,
                run_id=run_footer.run_id,
                admin_base_url=run_footer.admin_base_url,
            )
            final_text = f"{body_text}\n{footer.text}" if body_text else footer.text
            html_body = rendered if rendered else ""
            final_html = f"{html_body}\n{footer.html}" if html_body else footer.html

        return NormalizedOutboundEmail(
            from_email=from_email,
            to_emails=[inbound.from_email],
            subject=_re_prefixed(inbound.subject),
            body_text=final_text,
            body_html=final_html,
            message_id_header=message_id_factory(),
            in_reply_to_header=inbound.message_id_header,
            references_headers=[*inbound.references_headers, inbound.message_id_header],
            attachments=attachments,
        )


def _re_prefixed(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


__all__ = ["ReplyEnvelopeBuilder", "RunFooterContext", "render_markdown_to_html"]
