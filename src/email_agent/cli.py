import asyncio
import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(help="Email Assistant operator CLI", no_args_is_help=True)


@app.command()
def hello(name: str = typer.Option("world", help="Who to greet")) -> None:
    """Smoke command — confirms the CLI is wired."""
    typer.echo(f"hello, {name}")


@app.command("inject-email")
def inject_email(
    fixture: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help="Path to a .eml fixture to feed into the runtime.",
    ),
    to: str | None = typer.Option(
        None,
        "--to",
        help="Override the To: address (route to a specific assistant inbound).",
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        help="After accept_inbound, run execute_run synchronously and print the result.",
    ),
    use_real_model: bool = typer.Option(
        True,
        "--real-model/--no-real-model",
        help="Wire Fireworks (requires FIREWORKS_API_KEY). Disable to fail fast without an API key.",
    ),
    use_docker_sandbox: bool = typer.Option(
        True,
        "--docker/--in-memory",
        help="Use the docker sandbox (default) or the in-memory one (subprocess on host).",
    ),
) -> None:
    """Inject a `.eml` fixture into the runtime — fixture-driven local dev.

    Without `--follow`: just runs `accept_inbound` (router → thread → persist
    → queue an agent_runs row). With `--follow`: also runs `execute_run`
    synchronously and prints the rendered reply, agent run trace, token
    usage, and where the would-be outbound went (always InMemory — never
    sends real mail).
    """
    asyncio.run(
        _inject_email(
            fixture,
            to=to,
            follow=follow,
            use_real_model=use_real_model,
            use_docker_sandbox=use_docker_sandbox,
        )
    )


async def _inject_email(
    fixture: Path,
    *,
    to: str | None,
    follow: bool,
    use_real_model: bool,
    use_docker_sandbox: bool,
) -> None:
    from sqlalchemy import select

    from email_agent.composition import make_runtime_for_inject
    from email_agent.config import Settings
    from email_agent.db.models import AgentRun, RunStep, UsageLedger
    from email_agent.db.session import make_engine, make_session_factory
    from email_agent.mail.eml import parse_eml_file
    from email_agent.runtime.assistant_runtime import (
        Accepted,
        BudgetLimited,
        Completed,
        Dropped,
        Failed,
    )

    settings = Settings()  # ty: ignore[missing-argument]
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    email = parse_eml_file(fixture)
    if to is not None:
        email = email.model_copy(update={"to_emails": [to]})

    typer.echo(f"→ {email.from_email} → {email.to_emails!r}  subject={email.subject!r}")

    runtime, email_provider = make_runtime_for_inject(
        settings,
        session_factory,
        use_real_model=use_real_model,
        use_docker_sandbox=use_docker_sandbox,
    )

    accept = await runtime.accept_inbound(email)
    if isinstance(accept, Dropped):
        typer.secho(f"DROPPED  reason={accept.reason.value}  detail={accept.detail}", fg="red")
        raise typer.Exit(1)
    assert isinstance(accept, Accepted)
    typer.secho(
        f"ACCEPTED assistant={accept.assistant_id}  thread={accept.thread_id}  "
        f"message={accept.message_id}  created={accept.created}",
        fg="green",
    )

    if not follow:
        return

    # Look up the queued AgentRun keyed on this inbound.
    async with session_factory() as session:
        run_id = (
            await session.execute(
                select(AgentRun.id).where(AgentRun.inbound_message_id == accept.message_id)
            )
        ).scalar_one()

    typer.echo(f"\n--- executing run {run_id} ---\n")
    try:
        outcome = await runtime.execute_run(run_id)
    except Exception as exc:
        typer.secho(f"FAILED   {exc}", fg="red")
        raise typer.Exit(1) from exc

    if isinstance(outcome, Completed):
        typer.secho("COMPLETED", fg="green")
    elif isinstance(outcome, BudgetLimited):
        typer.secho("BUDGET_LIMITED", fg="yellow")
    elif isinstance(outcome, Failed):
        typer.secho(f"FAILED   {outcome.error}", fg="red")

    # Print what the email provider received (InMemory — nothing actually sent).
    sent_list = getattr(email_provider, "sent", [])
    if sent_list:
        sent = sent_list[-1]
        typer.echo("\n--- reply envelope ---")
        typer.echo(f"From:    {sent.from_email}")
        typer.echo(f"To:      {sent.to_emails}")
        typer.echo(f"Subject: {sent.subject}")
        typer.echo(f"In-Reply-To: {sent.in_reply_to_header}")
        typer.echo(f"References:  {sent.references_headers}")
        typer.echo(f"Attachments: {len(sent.attachments)}")
        typer.echo("\n--- body ---")
        typer.echo(sent.body_text)

    # Print run trace + usage from the DB.
    async with session_factory() as session:
        steps = (
            (await session.execute(select(RunStep).where(RunStep.run_id == run_id))).scalars().all()
        )
        usage = (
            (await session.execute(select(UsageLedger).where(UsageLedger.run_id == run_id)))
            .scalars()
            .all()
        )

    if steps:
        typer.echo("\n--- run steps ---")
        for step in steps:
            typer.echo(
                f"  [{step.kind:>5}]  in={step.input_summary[:80]!r}  out={step.output_summary[:80]!r}  cost_cents={step.cost_cents}"
            )

    if usage:
        u = usage[0]
        typer.echo(
            f"\n--- usage ---  input={u.input_tokens}  output={u.output_tokens}  cost_cents={u.cost_cents}  model={u.model}"
        )


@app.command("seed-assistant")
def seed_assistant(
    inbound_address: str = typer.Option(
        ...,
        "--inbound-address",
        help="The address Mailgun routes to this assistant (e.g. rose@mg.example.com).",
    ),
    end_user_email: str = typer.Option(
        ...,
        "--end-user",
        help="Email of the person this assistant talks to (the only allowed sender by default).",
    ),
    end_user_name: str | None = typer.Option(None, "--end-user-name", help="Display name."),
    owner_name: str = typer.Option(
        "Operator", "--owner-name", help="Owner row name (created if missing)."
    ),
    monthly_budget_cents: int = typer.Option(
        1000, "--monthly-budget-cents", help="Monthly cap in cents (default $10)."
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Model id to record on the assistant row. Defaults to FIREWORKS_MODEL_ID.",
    ),
    system_prompt: str | None = typer.Option(
        None,
        "--system-prompt",
        help="System prompt string. Mutually exclusive with --system-prompt-file.",
    ),
    system_prompt_file: Path | None = typer.Option(
        None,
        "--system-prompt-file",
        exists=True,
        readable=True,
        help="Read the system prompt from this file.",
    ),
    allowed_senders: list[str] = typer.Option(
        None,
        "--allowed-sender",
        help="Allowed sender (repeatable). Defaults to [end_user_email].",
    ),
    memory_namespace: str | None = typer.Option(
        None, "--memory-namespace", help="Cognee namespace. Defaults to assistant id."
    ),
) -> None:
    """Idempotent: create owner / end-user / assistant / scope / budget rows.

    Re-running with the same `--inbound-address` is a no-op (prints the
    existing assistant id). Useful for getting an assistant into the DB
    so `inject-email` can route to it.
    """
    if system_prompt and system_prompt_file:
        typer.secho("Use --system-prompt OR --system-prompt-file, not both.", fg="red")
        raise typer.Exit(2)
    prompt_text = (
        system_prompt_file.read_text()
        if system_prompt_file
        else (system_prompt or "be helpful and concise.")
    )
    senders = list(allowed_senders) if allowed_senders else [end_user_email]
    asyncio.run(
        _seed_assistant(
            inbound_address=inbound_address,
            end_user_email=end_user_email,
            end_user_name=end_user_name,
            owner_name=owner_name,
            monthly_budget_cents=monthly_budget_cents,
            model=model,
            system_prompt=prompt_text,
            allowed_senders=senders,
            memory_namespace=memory_namespace,
        )
    )


async def _seed_assistant(
    *,
    inbound_address: str,
    end_user_email: str,
    end_user_name: str | None,
    owner_name: str,
    monthly_budget_cents: int,
    model: str | None,
    system_prompt: str,
    allowed_senders: list[str],
    memory_namespace: str | None,
) -> None:
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import select

    from email_agent.config import Settings
    from email_agent.db.models import (
        Assistant,
        AssistantScopeRow,
        Budget,
        EndUser,
        Owner,
    )
    from email_agent.db.session import make_engine, make_session_factory

    settings = Settings()  # ty: ignore[missing-argument]
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    model_id = model or settings.fireworks_model_id

    async with session_factory() as session:
        # Idempotent on inbound_address.
        existing = (
            await session.execute(
                select(Assistant).where(Assistant.inbound_address == inbound_address)
            )
        ).scalar_one_or_none()
        if existing is not None:
            typer.secho(
                f"already seeded: assistant={existing.id}  inbound={inbound_address}",
                fg="yellow",
            )
            return

        owner = (
            await session.execute(select(Owner).where(Owner.name == owner_name))
        ).scalar_one_or_none()
        if owner is None:
            owner = Owner(id=f"o-{uuid.uuid4().hex[:8]}", name=owner_name)
            session.add(owner)
            await session.flush()

        end_user = (
            await session.execute(select(EndUser).where(EndUser.email == end_user_email))
        ).scalar_one_or_none()
        if end_user is None:
            end_user = EndUser(
                id=f"u-{uuid.uuid4().hex[:8]}",
                owner_id=owner.id,
                email=end_user_email,
                display_name=end_user_name,
            )
            session.add(end_user)
            await session.flush()

        assistant_id = f"a-{uuid.uuid4().hex[:8]}"
        budget_id = f"b-{uuid.uuid4().hex[:8]}"

        period_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Roll period_resets_at to the first of next month.
        if period_start.month == 12:
            period_reset = period_start.replace(year=period_start.year + 1, month=1)
        else:
            period_reset = period_start.replace(month=period_start.month + 1)

        session.add(
            Budget(
                id=budget_id,
                assistant_id=assistant_id,
                monthly_limit_cents=monthly_budget_cents,
                period_starts_at=period_start,
                period_resets_at=period_reset,
            )
        )
        session.add(
            Assistant(
                id=assistant_id,
                end_user_id=end_user.id,
                inbound_address=inbound_address,
                status="active",
                allowed_senders=allowed_senders,
                model=model_id,
                system_prompt=system_prompt,
            )
        )
        session.add(
            AssistantScopeRow(
                assistant_id=assistant_id,
                memory_namespace=memory_namespace or assistant_id,
                tool_allowlist=["read", "write", "edit", "bash", "memory_search", "attach_file"],
                budget_id=budget_id,
            )
        )
        await session.commit()

    typer.secho(
        f"seeded: assistant={assistant_id}  owner={owner.id}  end_user={end_user.id}  "
        f"budget={budget_id} ({monthly_budget_cents}c/mo)",
        fg="green",
    )


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
