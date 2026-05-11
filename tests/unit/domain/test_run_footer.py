from decimal import Decimal

from email_agent.domain.run_footer import (
    FOOTER_MARKER,
    render_run_footer,
    strip_footer,
)
from email_agent.models.agent import RunUsage


def _usage(input_tokens: int = 1234, output_tokens: int = 56, cost: str = "0.0042") -> RunUsage:
    return RunUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=Decimal(cost),
    )


def test_text_footer_contains_marker_run_id_tokens_and_cost() -> None:
    rendered = render_run_footer(
        _usage(),
        run_id="run-abc",
        admin_base_url="https://admin.example.com",
    )

    assert FOOTER_MARKER in rendered.text
    assert "Run: run-abc" in rendered.text
    assert "Tokens: in=1234 out=56" in rendered.text
    assert "Cost: $0.0042" in rendered.text
    assert "Admin: https://admin.example.com/admin/runs/run-abc" in rendered.text


def test_html_footer_includes_hr_marker_and_link() -> None:
    rendered = render_run_footer(
        _usage(),
        run_id="run-abc",
        admin_base_url="https://admin.example.com",
    )

    assert "<hr/>" in rendered.html
    assert FOOTER_MARKER in rendered.html
    assert "Run: run-abc" in rendered.html
    assert "in=1234 out=56" in rendered.html
    assert "$0.0042" in rendered.html
    assert (
        '<a href="https://admin.example.com/admin/runs/run-abc">'
        "https://admin.example.com/admin/runs/run-abc</a>"
    ) in rendered.html


def test_footer_omits_admin_link_when_base_url_unset() -> None:
    rendered = render_run_footer(_usage(), run_id="run-abc", admin_base_url=None)

    assert "Admin:" not in rendered.text
    assert "/admin/runs/" not in rendered.html
    assert "<a " not in rendered.html


def test_admin_base_url_trailing_slash_does_not_double() -> None:
    rendered = render_run_footer(
        _usage(),
        run_id="run-abc",
        admin_base_url="https://admin.example.com/",
    )

    assert "https://admin.example.com/admin/runs/run-abc" in rendered.text
    assert "https://admin.example.com//" not in rendered.text


def test_strip_footer_returns_body_up_to_marker() -> None:
    body = f"hello there\n\nmore reply\n{FOOTER_MARKER}\nRun: x\nCost: $0.0001\n"
    assert strip_footer(body) == "hello there\n\nmore reply\n"


def test_strip_footer_returns_body_unchanged_when_marker_absent() -> None:
    body = "no marker here, just chat"
    assert strip_footer(body) == body


def test_strip_footer_handles_single_level_quoted_marker() -> None:
    body = f"Thanks!\n\nOn Mon someone wrote:\n> {FOOTER_MARKER}\n> Run: run-abc\n> Cost: $0.0042\n"
    result = strip_footer(body)
    assert FOOTER_MARKER not in result
    assert result == "Thanks!\n\nOn Mon someone wrote:\n"


def test_strip_footer_handles_double_quoted_marker() -> None:
    body = f"top\n>> {FOOTER_MARKER}\n>> Run: x\n"
    result = strip_footer(body)
    assert result == "top\n"


def test_strip_footer_cuts_at_first_marker_only() -> None:
    body = f"first reply\n{FOOTER_MARKER}\nRun: r1\nold quoted\n> {FOOTER_MARKER}\n> Run: r0\n"
    result = strip_footer(body)
    assert result == "first reply\n"


def test_strip_footer_marker_at_start_returns_empty() -> None:
    body = f"{FOOTER_MARKER}\nRun: r1\n"
    assert strip_footer(body) == ""
