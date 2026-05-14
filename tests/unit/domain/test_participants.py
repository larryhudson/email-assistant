from email_agent.domain.participants import render_participants_block


def test_participants_block_lists_both_roles_when_distinct() -> None:
    block = render_participants_block(
        owner_email="owner@example.com",
        end_user_email="mum@example.com",
    )

    # Both roles + both emails present so the agent can identify either sender
    # without any names being hardcoded into the assistant's system prompt.
    assert "owner" in block.lower()
    assert "owner@example.com" in block
    assert "end_user" in block.lower() or "end-user" in block.lower()
    assert "mum@example.com" in block


def test_participants_block_collapses_when_owner_is_end_user() -> None:
    """Single-tenant personal assistant: the owner emails their own assistant.
    The block should describe only one sender so the agent doesn't think two
    distinct people are in scope."""
    block = render_participants_block(
        owner_email="me@example.com",
        end_user_email="me@example.com",
    )

    assert block.count("me@example.com") == 1
    assert "end_user" not in block.lower()
    assert "owner@example.com" not in block


def test_participants_block_is_empty_when_both_missing() -> None:
    """If neither email is set, render nothing rather than a confusing stub."""
    assert render_participants_block(owner_email="", end_user_email="") == ""
