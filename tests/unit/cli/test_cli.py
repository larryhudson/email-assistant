from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.agent.tool_registry import SUPPORTED_ASSISTANT_TOOLS
from email_agent.cli import _get_or_create_owner, _seed_assistant, app
from email_agent.db.models import Assistant, AssistantScopeRow, Owner
from email_agent.models.assistant import AssistantScope, AssistantStatus

runner = CliRunner()


def test_app_help_lists_expected_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("migrate", "hello"):
        assert cmd in result.stdout


def test_hello_prints_greeting():
    result = runner.invoke(app, ["hello", "--name", "Mum"])
    assert result.exit_code == 0
    assert "Mum" in result.stdout


async def test_get_or_create_owner_uses_email_when_names_are_duplicated(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        session.add(Owner(id="o-1", name="Larry", email="larry@example.com"))
        session.add(Owner(id="o-2", name="Larry", email=""))
        await session.commit()

    async with sqlite_session_factory() as session:
        owner = await _get_or_create_owner(
            session,
            owner_name="Larry",
            owner_email="larry@example.com",
        )

    assert owner.id == "o-1"


def test_supported_tool_registry_matches_all_agent_tools():
    scope = AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        owner_email="owner@example.com",
        end_user_id="u-1",
        end_user_email="user@example.com",
        inbound_address="assistant@example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("user@example.com",),
        tool_allowlist=SUPPORTED_ASSISTANT_TOOLS,
        memory_namespace="a-1",
        budget_id="b-1",
        model_name="test-model",
    )

    agent = AssistantAgent(has_memory=True, has_web_search=True, has_document_tools=True)
    built = agent._agent_for(scope)

    assert set(built._function_toolset.tools) == set(SUPPORTED_ASSISTANT_TOOLS)


async def test_seed_assistant_grants_all_supported_tools(
    monkeypatch,
    sqlite_engine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    class StubSettings:
        fireworks_model_id = "test-model"

    monkeypatch.setattr("email_agent.config.Settings", StubSettings)
    monkeypatch.setattr("email_agent.db.session.make_engine", lambda _settings: sqlite_engine)
    monkeypatch.setattr(
        "email_agent.db.session.make_session_factory",
        lambda _engine: sqlite_session_factory,
    )

    await _seed_assistant(
        inbound_address="assistant@example.com",
        end_user_email="user@example.com",
        end_user_name=None,
        owner_name="Owner",
        owner_email="owner@example.com",
        monthly_budget_usd=10,
        model=None,
        allowed_senders=["user@example.com"],
        memory_namespace=None,
    )

    async with sqlite_session_factory() as session:
        assistant = (
            await session.execute(
                select(Assistant).where(Assistant.inbound_address == "assistant@example.com")
            )
        ).scalar_one_or_none()
        assert assistant is not None
        scope = await session.get(AssistantScopeRow, assistant.id)

    assert scope is not None
    assert set(scope.tool_allowlist) == set(SUPPORTED_ASSISTANT_TOOLS)
