import subprocess
import sys

import typer

app = typer.Typer(help="Email Assistant operator CLI", no_args_is_help=True)


@app.command()
def hello(name: str = typer.Option("world", help="Who to greet")) -> None:
    """Smoke command — confirms the CLI is wired."""
    typer.echo(f"hello, {name}")


@app.command()
def migrate() -> None:
    """Run `alembic upgrade head`."""
    code = subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"])
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
