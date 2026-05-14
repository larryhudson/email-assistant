"""Renders a participants block for the agent's system prompt.

Identity (owner / end_user emails) flows in from the `Owner` + `EndUser`
rows on every run, instead of being hardcoded into each assistant's
`system_prompt`. That keeps the stored prompt generic, lets the same
template serve multiple assistants, and means changing a user's email
is a single SQL update — no prompt edit.
"""


def render_participants_block(*, owner_email: str, end_user_email: str) -> str:
    """Render the role + email participants block for the system prompt.

    Three shapes:
      - distinct owner + end_user → list both roles, mention cc routing.
      - owner == end_user (personal assistant) → single-sender block, no cc.
      - both missing → empty string (no block in the prompt).
    """
    owner = owner_email.strip()
    end_user = end_user_email.strip()
    if not owner and not end_user:
        return ""

    if owner and owner.lower() == end_user.lower():
        return f"# Participants\n\nThis is a personal assistant — one allowed sender: {owner}.\n"

    return (
        "# Participants\n"
        "\n"
        "Two people are allowed to email this assistant. Always check the "
        "inbound's `from:` header to identify the sender before replying.\n"
        "\n"
        f"- **end_user (primary):** {end_user}\n"
        "  This is the person the assistant exists for. Default to addressing "
        "replies to them and acting in their interest.\n"
        f"- **owner (admin):** {owner}\n"
        "  Configuration / maintenance role: prompt updates, debugging, "
        'scheduling tasks, "test" messages. Don\'t conflate admin notes with '
        "end-user requests and don't share the end-user's private context "
        "back to the owner unless they explicitly ask.\n"
        "\n"
        "**Reply routing:** when the owner emails this assistant, the runtime "
        "automatically replies to the owner and cc's the end-user, so the "
        "end-user sees admin threads. When the end-user emails, the reply has "
        "no cc. You don't set this yourself — the runtime handles it. Just be "
        "aware: in an owner-initiated thread, the end-user will read what you "
        "write.\n"
    )


__all__ = ["render_participants_block"]
