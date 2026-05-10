from contextlib import contextmanager

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel

from email_agent.models.agent import AgentDeps, AgentResult, RunUsage
from email_agent.models.assistant import AssistantScope


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
        # wiring (DeepSeek via OpenAI-compatible provider) lands when the
        # runtime composes things in slice 5's later tasks.
        agent: Agent[AgentDeps, str] = Agent(
            model=TestModel(),
            deps_type=AgentDeps,
            output_type=str,
            instructions=scope.system_prompt,
        )
        return agent

    @contextmanager
    def override_model(self, scope: AssistantScope, model: Model):
        """Override the agent's model for the duration of the block.

        Wraps PydanticAI's `Agent.override(model=...)` so callers don't need
        to know about the cache key. Used by tests with `TestModel` /
        `FunctionModel` and by the runtime when wiring DeepSeek.
        """
        agent = self._agent_for(scope)
        with agent.override(model=model):
            yield agent

    async def run(self, scope: AssistantScope, *, prompt: str, deps: AgentDeps) -> AgentResult:
        agent = self._agent_for(scope)
        result = await agent.run(prompt, deps=deps)
        usage = result.usage()
        return AgentResult(
            body=result.output,
            usage=RunUsage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cost_cents=0,
            ),
        )


__all__ = ["AssistantAgent"]
