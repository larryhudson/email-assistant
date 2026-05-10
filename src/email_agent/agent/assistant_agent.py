from contextlib import contextmanager

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel

from email_agent.agent.pricing import estimate_cost_cents
from email_agent.models.agent import AgentDeps, AgentResult, RunUsage
from email_agent.models.assistant import AssistantScope
from email_agent.models.memory import Memory
from email_agent.models.sandbox import BashResult, PendingAttachment, ToolCall


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
            instructions=scope.system_prompt,
        )

        @agent.tool
        async def read(ctx: RunContext[AgentDeps], path: str) -> str:
            """Read a file inside /workspace and return its text contents."""
            result = await ctx.deps.sandbox.run_tool(
                ctx.deps.assistant_id,
                ctx.deps.run_id,
                ToolCall(kind="read", path=path),
            )
            if not result.ok:
                raise RuntimeError(result.error or f"read({path}) failed")
            assert isinstance(result.output, str)
            return result.output

        @agent.tool
        async def write(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
            """Write a file inside /workspace. Refuses paths under /workspace/emails/."""
            result = await ctx.deps.sandbox.run_tool(
                ctx.deps.assistant_id,
                ctx.deps.run_id,
                ToolCall(kind="write", path=path, content=content),
            )
            if not result.ok:
                raise RuntimeError(result.error or f"write({path}) failed")
            return f"wrote {path}"

        @agent.tool
        async def edit(ctx: RunContext[AgentDeps], path: str, old: str, new: str) -> str:
            """Replace the first occurrence of `old` with `new` in `path`."""
            result = await ctx.deps.sandbox.run_tool(
                ctx.deps.assistant_id,
                ctx.deps.run_id,
                ToolCall(kind="edit", path=path, old=old, new=new),
            )
            if not result.ok:
                raise RuntimeError(result.error or f"edit({path}) failed")
            return f"edited {path}"

        @agent.tool
        async def attach_file(
            ctx: RunContext[AgentDeps], path: str, filename: str | None = None
        ) -> str:
            """Mark a file in /workspace as an attachment for the outgoing reply.

            Validates the file exists in the sandbox; the runtime reads the
            bytes back out after the run completes and stitches them into the
            outbound envelope. `filename` defaults to the basename of `path`.
            """
            check = await ctx.deps.sandbox.run_tool(
                ctx.deps.assistant_id,
                ctx.deps.run_id,
                ToolCall(kind="attach_file", path=path),
            )
            if not check.ok:
                raise RuntimeError(check.error or f"attach_file({path}) failed")
            ctx.deps.pending_attachments.append(
                PendingAttachment(
                    sandbox_path=path,
                    filename=filename or path.rsplit("/", 1)[-1],
                )
            )
            return f"attached {path}"

        @agent.tool
        async def memory_search(ctx: RunContext[AgentDeps], query: str) -> list[Memory]:
            """Search durable memory for the assistant; bypasses the sandbox."""
            return await ctx.deps.memory.search(ctx.deps.assistant_id, query)

        @agent.tool
        async def bash(ctx: RunContext[AgentDeps], command: str) -> str:
            """Run a bash command in the sandbox; returns combined stdout/stderr."""
            result = await ctx.deps.sandbox.run_tool(
                ctx.deps.assistant_id,
                ctx.deps.run_id,
                ToolCall(kind="bash", command=command),
            )
            if isinstance(result.output, BashResult):
                return (
                    f"exit_code={result.output.exit_code}\n"
                    f"stdout:\n{result.output.stdout}\n"
                    f"stderr:\n{result.output.stderr}"
                )
            if not result.ok:
                raise RuntimeError(result.error or "bash failed")
            return ""

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
                cost_cents=estimate_cost_cents(
                    model=scope.model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                ),
            ),
        )


__all__ = ["AssistantAgent"]
