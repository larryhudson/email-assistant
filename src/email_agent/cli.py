import asyncio
import shlex
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
    use_real_memory: bool = typer.Option(
        True,
        "--cognee-memory/--in-memory-memory",
        help="Use Cognee for durable memory (requires COGNEE_*_API_KEY). "
        "Disable for offline iteration without an embedding API key.",
    ),
    queue: bool = typer.Option(
        False,
        "--queue",
        help="Queue the run as a Procrastinate job instead of executing in-process. "
        "Requires `email-agent worker` running. Mutually exclusive with --follow.",
    ),
) -> None:
    """Inject a `.eml` fixture into the runtime — fixture-driven local dev.

    Without `--follow`: just runs `accept_inbound` (router → thread → persist
    → queue an agent_runs row). With `--follow`: also runs `execute_run`
    synchronously and prints the rendered reply, agent run trace, token
    usage, and where the would-be outbound went (always InMemory — never
    sends real mail). With `--queue`: enables Procrastinate so accept_inbound
    enqueues a run_agent job for a separate worker to pick up.
    """
    if queue and follow:
        typer.secho("--queue and --follow are mutually exclusive.", fg="red")
        raise typer.Exit(2)
    asyncio.run(
        _inject_email(
            fixture,
            to=to,
            follow=follow,
            use_real_model=use_real_model,
            use_docker_sandbox=use_docker_sandbox,
            use_real_memory=use_real_memory,
            queue=queue,
        )
    )


async def _inject_email(
    fixture: Path,
    *,
    to: str | None,
    follow: bool,
    use_real_model: bool,
    use_docker_sandbox: bool,
    use_real_memory: bool,
    queue: bool,
) -> None:
    from sqlalchemy import select

    from email_agent.composition import inject_session
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

    async with inject_session(
        settings,
        session_factory,
        use_real_model=use_real_model,
        use_docker_sandbox=use_docker_sandbox,
        use_real_memory=use_real_memory,
        use_procrastinate=queue,
    ) as (runtime, email_provider):
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
                f"  [{step.kind:>5}]  in={step.input_summary[:80]!r}  out={step.output_summary[:80]!r}  cost=${step.cost_usd}"
            )

    if usage:
        u = usage[0]
        typer.echo(
            f"\n--- usage ---  input={u.input_tokens}  output={u.output_tokens}  cost=${u.cost_usd}  model={u.model}"
        )


@app.command("seed-assistant")
def seed_assistant(
    inbound_address: str = typer.Option(
        ...,
        "--inbound-address",
        help="The address Mailgun routes to this assistant (e.g. sam@mg.example.com).",
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
    monthly_budget_usd: float = typer.Option(
        10.00, "--monthly-budget-usd", help="Monthly cap in USD (default $10.00)."
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
    from decimal import Decimal as _Decimal

    asyncio.run(
        _seed_assistant(
            inbound_address=inbound_address,
            end_user_email=end_user_email,
            end_user_name=end_user_name,
            owner_name=owner_name,
            monthly_budget_usd=_Decimal(str(monthly_budget_usd)),
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
    monthly_budget_usd,  # Decimal at runtime (untyped to keep imports lazy in this module)
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
                monthly_limit_usd=monthly_budget_usd,
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
        f"budget={budget_id} (${monthly_budget_usd}/mo)",
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


@app.command("seed-memory")
def seed_memory(
    assistant_id: str = typer.Option(
        ...,
        "--assistant",
        help="Assistant id (e.g. a-3f5aad0e). Memory is stored under that "
        "assistant's per-assistant cognee root.",
    ),
    fact: str = typer.Option(
        ...,
        "--fact",
        help="The fact to remember. Plain text — anything cognee can ingest.",
    ),
    session_id: str | None = typer.Option(
        None,
        "--session-id",
        help="Optional thread/session id. Without it, the fact lands in the "
        "durable graph (recallable across all threads).",
    ),
) -> None:
    """Seed a fact into a specific assistant's cognee memory.

    Useful for demoing recall and bootstrapping an assistant with prior
    context before any real run. Stores under the same per-assistant data
    root the runtime uses, so subsequent inject-email runs will recall it.
    """
    from email_agent.composition import make_cognee_memory
    from email_agent.config import Settings
    from email_agent.memory.cognee import CogneeMemoryAdapter

    async def _run() -> None:
        settings = Settings()  # ty: ignore[missing-argument]
        memory = make_cognee_memory(settings)
        assert isinstance(memory, CogneeMemoryAdapter)
        if session_id is None:
            await memory.seed_durable(assistant_id, fact)
            where = "durable graph"
        else:
            await memory.record_turn(
                assistant_id=assistant_id,
                thread_id=session_id,
                role="seed",
                content=fact,
            )
            where = f"session={session_id}"
        typer.secho(f"seeded fact for assistant={assistant_id}  ({where})", fg="green")

    asyncio.run(_run())


@app.command("worker")
def worker(
    queues: list[str] | None = typer.Option(
        None,
        "--queue",
        help="Listen only on these queues (repeatable). Default: all queues.",
    ),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Wait indefinitely for new jobs (default). Use --no-wait to drain "
        "the queue once and exit — useful in tests.",
    ),
    concurrency: int = typer.Option(1, "--concurrency", help="Async jobs per worker."),
) -> None:
    """Run the Procrastinate worker that dispatches run_agent + curate_memory."""

    async def _run() -> None:
        from email_agent.jobs.app import app as procrastinate_app

        worker_kwargs: dict[str, object] = {"wait": wait, "concurrency": concurrency}
        if queues:
            worker_kwargs["queues"] = queues

        async with procrastinate_app.open_async():
            await procrastinate_app.run_worker_async(**worker_kwargs)  # ty: ignore[invalid-argument-type]

    asyncio.run(_run())


@app.command("worker-dev")
def worker_dev(
    queues: list[str] | None = typer.Option(
        None,
        "--queue",
        help="Listen only on these queues (repeatable). Default: all queues.",
    ),
    concurrency: int = typer.Option(1, "--concurrency", help="Async jobs per worker."),
    reload_dirs: list[Path] | None = typer.Option(
        None,
        "--reload-dir",
        help="Directory to watch for worker reloads (repeatable). Default: src.",
    ),
) -> None:
    """Run the Procrastinate worker with auto-reload for local development."""
    from watchfiles import Change, run_process

    command_args = ["-m", "email_agent.cli", "worker", "--concurrency", str(concurrency)]
    for queue in queues or []:
        command_args.extend(["--queue", queue])

    watch_paths = [str(path) for path in (reload_dirs or [Path("src")])]

    def _changed(changes: set[tuple[Change, str]]) -> None:
        changed = ", ".join(sorted({path for _, path in changes})[:3])
        suffix = f": {changed}" if changed else ""
        typer.secho(f"reloading worker{suffix}", fg="yellow")

    raise typer.Exit(
        run_process(
            *watch_paths,
            target=shlex.join([sys.executable, *command_args]),
            target_type="command",
            callback=_changed,
        )
    )


if __name__ == "__main__":
    app()
