from typer.testing import CliRunner

from email_agent.cli import app

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
