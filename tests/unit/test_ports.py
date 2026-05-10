from typing import get_type_hints

from email_agent.mail.port import EmailProvider
from email_agent.memory.port import MemoryPort
from email_agent.sandbox.port import AssistantSandbox


def test_email_provider_has_required_methods():
    for name in ("verify_webhook", "parse_inbound", "send_reply"):
        assert hasattr(EmailProvider, name)


def test_memory_port_has_required_methods():
    for name in ("recall", "record_turn", "search", "delete_assistant"):
        assert hasattr(MemoryPort, name)


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


def test_protocols_use_assistant_id_for_isolation():
    hints = get_type_hints(MemoryPort.recall)
    assert "assistant_id" in hints
    assert hints["assistant_id"] is str
