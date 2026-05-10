from typing import get_type_hints

from email_agent.memory.port import MemoryPort


def test_memory_port_has_required_methods():
    for name in ("recall", "record_turn", "search", "delete_assistant"):
        assert hasattr(MemoryPort, name)


def test_memory_port_uses_assistant_id_for_isolation():
    hints = get_type_hints(MemoryPort.recall)
    assert "assistant_id" in hints
    assert hints["assistant_id"] is str
