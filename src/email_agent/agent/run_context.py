from dataclasses import dataclass

from email_agent.models.memory import Memory


@dataclass(frozen=True)
class AgentPromptContext:
    prompt: str
    recalled_memory: list[Memory]
    current_message_path: str


class RunContextAssembler:
    """Builds the model-facing prompt context for a single email-agent run."""

    def build(
        self,
        *,
        current_message_path: str,
        memories: list[Memory],
        memory_enabled: bool = True,
    ) -> AgentPromptContext:
        memory_block = ""
        if memories:
            memory_block = "\n\nRecalled memory:\n" + "\n".join(
                f"- {memory.content}" for memory in memories
            )

        memory_sentence = "Use `memory_search` to look up prior context. " if memory_enabled else ""
        prompt = (
            f"A new inbound email has arrived. Read it from {current_message_path!r} "
            f"using the `read` tool. Your final response (a plain string returned from this run) "
            f"becomes the body of the reply email — do NOT write the reply to disk, and do NOT "
            f"modify anything under emails/ (that directory is the read-only thread history). "
            f"Use `write`/`edit`/`bash` only if you need scratch files under other paths. "
            f"{memory_sentence}Use `attach_file` only if you "
            f"genuinely need to attach a generated artefact." + memory_block
        )

        return AgentPromptContext(
            prompt=prompt,
            recalled_memory=memories,
            current_message_path=current_message_path,
        )


__all__ = ["AgentPromptContext", "RunContextAssembler"]
