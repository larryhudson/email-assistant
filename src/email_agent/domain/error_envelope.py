"""Envelope builders for run-failure notification emails.

Two shapes, intentionally separate:

- `build_end_user_error_envelope` threads into the inbound conversation
  (`Re:` + `In-Reply-To` + `References`) so the user sees an apologetic
  do-not-reply note inline with their thread. The body MUST NOT leak
  exception internals.
- `build_owner_error_envelope` is a fresh, unthreaded message to the
  assistant owner (the admin) — technical body with exception type,
  message, run id, and the admin run-detail URL when configured.

Neither envelope carries the cost-info footer — failed runs have no
meaningful usage to report and the footer marker exists for completed
replies only.
"""

from collections.abc import Callable

from email_agent.domain.reply_envelope import render_markdown_to_html
from email_agent.models.email import NormalizedInboundEmail, NormalizedOutboundEmail


def build_end_user_error_envelope(
    *,
    inbound: NormalizedInboundEmail,
    from_email: str,
    run_id: str,
    message_id_factory: Callable[[], str],
) -> NormalizedOutboundEmail:
    body = (
        "Sorry — something went wrong while processing your message and no "
        "reply was generated. The team has been notified.\n"
        "\n"
        "This is an automated notice; please do not reply.\n"
        "\n"
        f"Run: {run_id}"
    )
    return NormalizedOutboundEmail(
        from_email=from_email,
        to_emails=[inbound.from_email],
        subject=_re_prefixed(inbound.subject),
        body_text=body,
        body_html=render_markdown_to_html(body) or None,
        message_id_header=message_id_factory(),
        in_reply_to_header=inbound.message_id_header,
        references_headers=[*inbound.references_headers, inbound.message_id_header],
        attachments=[],
    )


def build_owner_error_envelope(
    *,
    owner_email: str,
    from_email: str,
    run_id: str,
    exception: BaseException,
    admin_base_url: str | None,
    message_id_factory: Callable[[], str],
) -> NormalizedOutboundEmail:
    exc_type = type(exception).__name__
    exc_message = str(exception)
    lines = [
        f"Agent run `{run_id}` failed.",
        "",
        f"Exception: {exc_type}: {exc_message}",
    ]
    if admin_base_url is not None:
        lines.extend(["", f"Admin: {_admin_url(admin_base_url, run_id)}"])
    body = "\n".join(lines)
    return NormalizedOutboundEmail(
        from_email=from_email,
        to_emails=[owner_email],
        subject=f"[email-agent] run {run_id} failed",
        body_text=body,
        body_html=render_markdown_to_html(body) or None,
        message_id_header=message_id_factory(),
        in_reply_to_header=None,
        references_headers=[],
        attachments=[],
    )


def _re_prefixed(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def _admin_url(base_url: str, run_id: str) -> str:
    return f"{base_url.rstrip('/')}/admin/runs/{run_id}"


__all__ = ["build_end_user_error_envelope", "build_owner_error_envelope"]
