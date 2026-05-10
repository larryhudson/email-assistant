from email_agent.sandbox.port import AssistantSandbox


def test_sandbox_has_required_methods():
    for name in (
        "ensure_started",
        "project_emails",
        "project_attachments",
        "run_tool",
        "read_attachment_out",
        "reset",
    ):
        assert hasattr(AssistantSandbox, name)
