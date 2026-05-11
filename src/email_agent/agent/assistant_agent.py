from contextlib import contextmanager
from decimal import Decimal

from pydantic_ai import Agent, RunContext, capture_run_messages
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
from email_agent.models.agent import (
    AgentDeps,
    AgentResult,
    AgentRunError,
    RunStepRecord,
    RunUsage,
)
from email_agent.models.assistant import AssistantScope
from email_agent.models.memory import Memory
from email_agent.models.scheduled import ScheduledTask
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

        @agent.tool
        async def list_scheduled_tasks(ctx: RunContext[AgentDeps]) -> list[ScheduledTask]:
            """List scheduled tasks for this assistant (both ONCE and CRON kinds)."""
            return await ctx.deps.toolset.list_scheduled_tasks()

        @agent.tool
        async def create_scheduled_task(
            ctx: RunContext[AgentDeps],
            kind: str,
            when: str,
            name: str,
            body: str,
        ) -> str:
            """Create a scheduled task that fires a synthetic inbound to this assistant.

            `kind` is 'once' or 'cron'. For 'once', `when` is an ISO-8601
            timezone-aware datetime (e.g. '2026-05-12T09:00:00+10:00'); for
            'cron', `when` is a 5-field cron expression (e.g. '0 9 * * *').
            `name` is a short label used as the synthetic inbound's subject;
            `body` is the prompt the agent will receive when the task fires.
            """
            return await ctx.deps.toolset.create_scheduled_task(kind, when, name, body)

        @agent.tool
        async def delete_scheduled_task(ctx: RunContext[AgentDeps], task_id: str) -> str:
            """Delete a scheduled task owned by this assistant by its id."""
            return await ctx.deps.toolset.delete_scheduled_task(task_id)

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
        # capture_run_messages retains the request/response log even when
        # agent.run raises — so a run that fails after N tool calls still
        # exposes N model responses (with per-request usage) and the tool
        # history. We use that to populate `AgentRunError` so the recorder
        # can persist partial usage + steps instead of dropping them.
        with capture_run_messages() as captured:
            try:
                result = await agent.run(prompt, deps=deps)
            except Exception as exc:
                partial_usage = _summarise_partial_usage(captured, scope)
                partial_steps = _extract_steps(list(captured))
                raise AgentRunError(exc, usage=partial_usage, steps=partial_steps) from exc
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


def _summarise_partial_usage(messages: list[ModelMessage], scope: AssistantScope) -> RunUsage:
    """Sum per-response usage across all ModelResponse messages captured so far.

    Each ModelResponse carries a RequestUsage; cumulative input/output tokens
    sum to the same totals as result.usage() on a completed run.
    """
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        usage = getattr(msg, "usage", None)
        if usage is None:
            continue
        input_tokens += usage.input_tokens or 0
        output_tokens += usage.output_tokens or 0
        cache_read_tokens += getattr(usage, "cache_read_tokens", 0) or 0
    return RunUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=estimate_cost_usd(
            model=scope.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        if input_tokens or output_tokens
        else Decimal("0"),
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
