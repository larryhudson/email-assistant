from dataclasses import dataclass
from html import escape

from email_agent.models.agent import RunUsage

# Module-level invariant: must appear verbatim in BOTH text and HTML renderings
# so `strip_footer` has a single contract for inbound stripping.
FOOTER_MARKER = "-- email-agent run footer --"


@dataclass(frozen=True)
class RenderedFooter:
    text: str
    html: str


def render_run_footer(
    usage: RunUsage,
    *,
    run_id: str,
    admin_base_url: str | None,
) -> RenderedFooter:
    """Render the cost/run footer appended to outbound replies.

    Marker appears as the first content line in plain text and inside an HTML
    comment + a visible `<p>` in HTML so a quoted reply ("> " prefix) still
    contains the marker on a line by itself for the stripper to find.
    """
    cost = f"${usage.cost_usd:.4f}"
    text_lines = [
        "",
        FOOTER_MARKER,
        f"Run: {run_id}",
        f"Tokens: in={usage.input_tokens} out={usage.output_tokens}",
        f"Cost: {cost}",
    ]
    html_parts = [
        "<hr/>",
        f"<!-- {FOOTER_MARKER} -->",
        f"<p>{escape(FOOTER_MARKER)}</p>",
        f"<p>Run: {escape(run_id)}</p>",
        f"<p>Tokens: in={usage.input_tokens} out={usage.output_tokens}</p>",
        f"<p>Cost: {escape(cost)}</p>",
    ]

    if admin_base_url is not None:
        admin_url = _admin_url(admin_base_url, run_id)
        text_lines.append(f"Admin: {admin_url}")
        html_parts.append(f'<p>Admin: <a href="{escape(admin_url)}">{escape(admin_url)}</a></p>')

    text = "\n".join(text_lines) + "\n"
    html = "\n".join(html_parts) + "\n"
    return RenderedFooter(text=text, html=html)


def strip_footer(body: str) -> str:
    """Return `body` up to (but not including) the first footer marker line.

    Handles plain markers and markers nested under one or more `> ` quote
    prefixes (Gmail/Apple Mail reply quoting). Marker absent → body unchanged.
    Marker at first line → empty string.
    """
    lines = body.splitlines(keepends=True)
    offset = 0
    for line in lines:
        if _is_marker_line(line):
            return body[:offset]
        offset += len(line)
    return body


def _is_marker_line(line: str) -> bool:
    stripped = line.rstrip("\r\n")
    # Peel off any number of leading "> " / ">" quote prefixes.
    while stripped.startswith(">"):
        stripped = stripped[1:]
        if stripped.startswith(" "):
            stripped = stripped[1:]
    return stripped.strip() == FOOTER_MARKER


def _admin_url(base_url: str, run_id: str) -> str:
    return f"{base_url.rstrip('/')}/admin/runs/{run_id}"


__all__ = ["FOOTER_MARKER", "RenderedFooter", "render_run_footer", "strip_footer"]
