from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.memory import Memory, MemoryContext
from email_agent.models.sandbox import (
    BashResult,
    PendingAttachment,
    ProjectedFile,
    ToolCall,
    ToolResult,
)


def test_assistant_scope_carries_owner_chain():
    scope = AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        end_user_id="u-1",
        inbound_address="assistant+mum@example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="a-1",
        tool_allowlist=("read", "write", "edit", "bash", "memory_search", "attach_file"),
        budget_id="b-1",
        model_name="deepseek-flash",
        system_prompt="You are kind.",
    )
    assert scope.is_sender_allowed("mum@example.com")
    assert not scope.is_sender_allowed("spam@example.com")
    assert scope.is_sender_allowed("MUM@example.com")  # case-insensitive


def test_assistant_scope_rejects_mutation():
    scope = AssistantScope(
        assistant_id="a",
        owner_id="o",
        end_user_id="u",
        inbound_address="x@y",
        status=AssistantStatus.ACTIVE,
        allowed_senders=(),
        memory_namespace="a",
        tool_allowlist=(),
        budget_id="b",
        model_name="m",
        system_prompt="p",
    )
    with pytest.raises(ValidationError):
        scope.status = AssistantStatus.PAUSED


def test_memory_and_context():
    m = Memory(id="m-1", content="user prefers short replies", source_run_id="r-1")
    ctx = MemoryContext(memories=[m], retrieved_at=datetime.now(UTC))
    assert ctx.memories[0].content.endswith("short replies")


def test_tool_call_variants():
    read_call = ToolCall(kind="read", path="/workspace/x.md")
    write_call = ToolCall(kind="write", path="/workspace/y.md", content="hi")
    bash_call = ToolCall(kind="bash", command="ls /workspace")
    assert read_call.kind == "read"
    assert write_call.content == "hi"
    assert bash_call.command == "ls /workspace"


def test_tool_call_rejects_missing_required_field():
    with pytest.raises((ValidationError, ValueError)):
        ToolCall(kind="write", path="/workspace/y.md")  # content missing


def test_bash_result_carries_streams():
    r = BashResult(exit_code=0, stdout="ok\n", stderr="", duration_ms=12)
    assert r.exit_code == 0


def test_tool_result_can_wrap_bash():
    r = ToolResult(ok=True, output=BashResult(exit_code=0, stdout="", stderr="", duration_ms=1))
    assert r.ok is True


def test_projected_file_holds_bytes():
    f = ProjectedFile(path="emails/0001-from-mum.md", content=b"---\nsubject: hi\n---\n")
    assert f.path.startswith("emails/")


def test_pending_attachment_records_filename():
    pa = PendingAttachment(sandbox_path="/workspace/out.pdf", filename="report.pdf")
    assert pa.filename == "report.pdf"
