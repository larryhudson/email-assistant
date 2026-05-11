from email_agent.domain.reply_envelope import render_markdown_to_html


def test_renders_headings_as_html_tags():
    html = render_markdown_to_html("# Title\n\nbody")
    assert "<h1>Title</h1>" in html


def test_renders_bullet_list():
    html = render_markdown_to_html("- one\n- two\n")
    assert "<ul>" in html
    assert "</ul>" in html
    assert "<li>one</li>" in html
    assert "<li>two</li>" in html


def test_renders_inline_code():
    html = render_markdown_to_html("use `x = 1` here")
    assert "<code>x = 1</code>" in html


def test_renders_fenced_code_block():
    html = render_markdown_to_html("```\nprint(1)\n```\n")
    assert "<pre>" in html
    assert "<code>" in html
    assert "print(1)" in html


def test_renders_links():
    html = render_markdown_to_html("[text](https://example.com)")
    assert '<a href="https://example.com">text</a>' in html


def test_renders_bold_and_italic():
    html = render_markdown_to_html("**bold** and *italic*")
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html


def test_paragraphs_separated_by_blank_line():
    html = render_markdown_to_html("first\n\nsecond")
    assert "<p>first</p>" in html
    assert "<p>second</p>" in html


def test_empty_body_renders_to_empty_string():
    assert render_markdown_to_html("") == ""


def test_whitespace_only_body_renders_to_empty_string():
    assert render_markdown_to_html("   \n\n  \n").strip() == ""


def test_escapes_html_special_characters_in_literal_text():
    html = render_markdown_to_html("a < b & c > d")
    assert "&lt;" in html
    assert "&gt;" in html
    assert "&amp;" in html
    # Raw HTML must not pass through verbatim.
    assert "<b>" not in html


def test_raw_html_in_markdown_is_escaped_not_passed_through():
    html = render_markdown_to_html("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html or "alert(1)" in html


def test_renders_pipe_table_to_html_table():
    md = "| Option | Pro | Con |\n|---|---|---|\n| A | fast | risky |\n| B | slow | safe |\n"
    html = render_markdown_to_html(md)
    assert "<table>" in html
    assert "<thead>" in html
    # Header cells use <th>.
    assert "<th>Option</th>" in html
    assert "<th>Pro</th>" in html
    # Body cells use <td>.
    assert "<td>A</td>" in html
    assert "<td>fast</td>" in html
    # No leftover pipes in the rendered output.
    assert "|" not in html


def test_renders_strikethrough_and_autolinks_bare_urls():
    """gfm-like preset turns ~~x~~ into <s> and bare URLs into <a href=...>."""
    html = render_markdown_to_html("~~old plan~~. See https://example.com for the new one.")
    assert "<s>old plan</s>" in html
    assert '<a href="https://example.com">https://example.com</a>' in html
