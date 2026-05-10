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


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(True, help="Auto-reload on file changes (dev default)"),
) -> None:
    """Run the FastAPI app (Mailgun webhook + future admin UI)."""
    import uvicorn

    uvicorn.run(
        "email_agent.web.app:build_app_from_settings",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=["src"] if reload else None,
        factory=True,
        log_config=None,
    )


if __name__ == "__main__":
    app()
