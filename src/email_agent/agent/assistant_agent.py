from contextlib import contextmanager

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel

from email_agent.agent.pricing import estimate_cost_usd
from email_agent.models.agent import AgentDeps, AgentResult, RunStepRecord, RunUsage
from email_agent.models.assistant import AssistantScope
from email_agent.models.memory import Memory
from email_agent.sandbox.skills import SYSTEM_PROMPT_GUIDANCE


class AssistantAgent:
    """Wraps a PydanticAI `Agent` per-assistant, plus the six tools.

    One `Agent` is built lazily per `(model_name, system_prompt)` pair and
    cached for the process lifetime. Tools are registered at build time;
    per-run state flows through `RunContext[AgentDeps]`.
    """

    def __init__(self) -> None:
        self._agents: dict[tuple[str, str], Agent[AgentDeps, str]] = {}

    def _agent_for(self, scope: AssistantScope) -> Agent[AgentDeps, str]:
        key = (scope.model_name, scope.system_prompt)
        cached = self._agents.get(key)
        if cached is not None:
            return cached

        agent = self._build_agent(scope)
        self._agents[key] = agent
        return agent

    def _build_agent(self, scope: AssistantScope) -> Agent[AgentDeps, str]:
        # Default model is a TestModel placeholder; production callers should
        # invoke `override_model(scope, real_model)` before `run`. Real model
        # wiring (Fireworks via OpenAI-compatible provider) lands when the
        # runtime composes things in slice 5's later tasks.
        agent: Agent[AgentDeps, str] = Agent(
            model=TestModel(),
            deps_type=AgentDeps,
            output_type=str,
            instructions=[scope.system_prompt, SYSTEM_PROMPT_GUIDANCE],
        )

        @agent.instructions
        def workspace_context(ctx: RunContext[AgentDeps]) -> str:
            parts = [
                ctx.deps.context_block.strip(),
                ctx.deps.skills_block.strip(),
            ]
            return "\n\n".join(p for p in parts if p)

        @agent.tool
        async def read(ctx: RunContext[AgentDeps], path: str) -> str:
            """Read a file inside /workspace and return its text contents."""
            return await ctx.deps.toolset.read(path)

        @agent.tool
        async def write(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
            """Write a file inside /workspace. Refuses paths under /workspace/emails/."""
            return await ctx.deps.toolset.write(path, content)

        @agent.tool
        async def edit(ctx: RunContext[AgentDeps], path: str, old: str, new: str) -> str:
            """Replace the first occurrence of `old` with `new` in `path`."""
            return await ctx.deps.toolset.edit(path, old, new)

        @agent.tool
        async def attach_file(
            ctx: RunContext[AgentDeps], path: str, filename: str | None = None
        ) -> str:
            """Mark a file in /workspace as an attachment for the outgoing reply.

            Validates the file exists in the sandbox; the runtime reads the
            bytes back out after the run completes and stitches them into the
            outbound envelope. `filename` defaults to the basename of `path`.
            """
            return await ctx.deps.toolset.attach_file(path, filename)

        @agent.tool
        async def memory_search(ctx: RunContext[AgentDeps], query: str) -> list[Memory]:
            """Search durable memory for the assistant; bypasses the sandbox."""
            return await ctx.deps.toolset.memory_search(query)

        @agent.tool
        async def bash(ctx: RunContext[AgentDeps], command: str) -> str:
            """Run a bash command in the sandbox; returns combined stdout/stderr."""
            return await ctx.deps.toolset.bash(command)

        return agent

    @contextmanager
    def override_model(self, scope: AssistantScope, model: Model):
        """Override the agent's model for the duration of the block.

        Wraps PydanticAI's `Agent.override(model=...)` so callers don't need
        to know about the cache key. Used by tests with `TestModel` /
        `FunctionModel` and by the runtime when wiring Fireworks.
        """
        agent = self._agent_for(scope)
        with agent.override(model=model):
            yield agent

    async def run(self, scope: AssistantScope, *, prompt: str, deps: AgentDeps) -> AgentResult:
        agent = self._agent_for(scope)
        result = await agent.run(prompt, deps=deps)
        usage = result.usage()
        input_tokens = usage.input_tokens or 0
        output_tokens = usage.output_tokens or 0
        cache_read_tokens = getattr(usage, "cache_read_tokens", 0) or 0
        return AgentResult(
            body=result.output,
            usage=RunUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=estimate_cost_usd(
                    model=scope.model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                ),
            ),
            steps=_extract_steps(result.all_messages()),
        )


def _extract_steps(messages: list[ModelMessage]) -> list[RunStepRecord]:
    """Walk PydanticAI's message history and emit one RunStep per event.

    Order: model text/tool-call parts (in arrival order) followed by their
    corresponding tool returns. Tool returns are matched to calls by
    `tool_call_id`. The final assistant text is emitted as a `model` step.
    """
    # Build a map of tool_call_id -> ToolReturnPart from request messages.
    returns: dict[str, ToolReturnPart] = {}
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                returns[part.tool_call_id] = part

    steps: list[RunStepRecord] = []
    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                ret = returns.get(part.tool_call_id)
                steps.append(
                    RunStepRecord(
                        kind=f"tool:{part.tool_name}",
                        input_summary=_truncate(_stringify(part.args)),
                        output_summary=(
                            _truncate(_stringify(ret.content)) if ret else "<no return>"
                        ),
                    )
                )
            elif isinstance(part, TextPart):
                steps.append(
                    RunStepRecord(
                        kind="model",
                        input_summary="",
                        output_summary=_truncate(part.content),
                    )
                )
    return steps


def _stringify(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        import json

        try:
            return json.dumps(value, default=str)
        except TypeError:
            return repr(value)
    return repr(value)


def _truncate(s: str, limit: int = 500) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


__all__ = ["AssistantAgent"]
