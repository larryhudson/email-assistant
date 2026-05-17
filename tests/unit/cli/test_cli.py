from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from email_agent.cli import _get_or_create_owner, app
from email_agent.db.models import Owner

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
