from email_agent.agent.run_context import RunContextAssembler
from email_agent.models.memory import Memory


def test_run_context_prompt_captures_email_agent_contract() -> None:
    context = RunContextAssembler().build(
        current_message_path="emails/t-1/0001.md",
        memories=[],
    )

    assert "emails/t-1/0001.md" in context.prompt
    assert "using the `read` tool" in context.prompt
    assert "becomes the body of the reply email" in context.prompt
    assert "do NOT write the reply to disk" in context.prompt
    assert "do NOT modify anything under emails/" in context.prompt
    assert "Use `memory_search`" in context.prompt
    assert "Use `attach_file` only if you genuinely need" in context.prompt
    assert context.current_message_path == "emails/t-1/0001.md"
    assert context.recalled_memory == []


def test_run_context_prompt_omits_memory_search_when_memory_disabled() -> None:
    context = RunContextAssembler().build(
        current_message_path="emails/t-1/0001.md",
        memories=[],
        memory_enabled=False,
    )

    assert "memory_search" not in context.prompt
    # Other guidance is still present.
    assert "using the `read` tool" in context.prompt
    assert "Use `attach_file` only if you genuinely need" in context.prompt


def test_run_context_prompt_includes_recalled_memory_block() -> None:
    memories = [
        Memory(id="m-1", content="Mum prefers concise replies.", score=0.9),
        Memory(id="m-2", content="Mention Sunday lunch when relevant.", score=0.8),
    ]

    context = RunContextAssembler().build(
        current_message_path="emails/t-1/0001.md",
        memories=memories,
    )

    assert "Recalled memory:" in context.prompt
    assert "- Mum prefers concise replies." in context.prompt
    assert "- Mention Sunday lunch when relevant." in context.prompt
    assert context.recalled_memory == memories
