import base64
import binascii
import json
import logging
import secrets
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from starlette.datastructures import UploadFile

if TYPE_CHECKING:
    from procrastinate import App as ProcrastinateApp
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.mail.mailgun import (
    MailgunEmailProvider,
    MailgunParseError,
    MailgunSignatureError,
)
from email_agent.models.email import WebhookRequest
from email_agent.runtime.assistant_runtime import (
    Accepted,
    AssistantRuntime,
    Dropped,
)


def _configure_logging() -> None:
    """Idempotent dev-friendly logging setup.

    Plain stderr handler at INFO with a short timestamp + level + logger name.
    Idempotent so uvicorn --reload doesn't keep stacking handlers.
    """
    root = logging.getLogger()
    if getattr(root, "_email_agent_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root._email_agent_configured = True  # ty: ignore[unresolved-attribute]


_configure_logging()
log = logging.getLogger("email_agent.web")


def build_app_from_settings() -> FastAPI:
    """Compose the production app from environment-backed settings.

    The webhook fast path only needs `accept_inbound`, so we construct a
    lightweight runtime (no sandbox, no agent, no model factory) — those
    are the worker's concern, set up in `jobs.app:build_worker_deps`. We
    DO wire `run_agent_defer` so accept_inbound enqueues a procrastinate
    job for the worker to pick up.
    """
    from email_agent.config import Settings
    from email_agent.db.session import make_engine, make_session_factory
    from email_agent.jobs.app import app as procrastinate_app
    from email_agent.jobs.app import defer_run_agent

    settings = Settings()  # ty: ignore[missing-argument]
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    provider = MailgunEmailProvider(
        signing_key=settings.mailgun_signing_key.get_secret_value(),
    )
    runtime = AssistantRuntime(
        factory,
        attachments_root=settings.attachments_root,
        run_agent_defer=defer_run_agent,
    )
    settings.attachments_root.mkdir(parents=True, exist_ok=True)
    return build_app(
        provider=provider,
        runtime=runtime,
        session_factory=factory,
        procrastinate_app=procrastinate_app,
        admin_basic_auth_username=settings.admin_basic_auth_username,
        admin_basic_auth_password=(
            settings.admin_basic_auth_password.get_secret_value()
            if settings.admin_basic_auth_password is not None
            else None
        ),
        admin_auth_required=True,
    )


def build_app(
    *,
    provider: MailgunEmailProvider,
    runtime: AssistantRuntime,
    session_factory: "async_sessionmaker[AsyncSession] | None" = None,
    procrastinate_app: "ProcrastinateApp | None" = None,
    admin_basic_auth_username: str | None = None,
    admin_basic_auth_password: str | None = None,
    admin_auth_required: bool = False,
) -> FastAPI:
    """Build the FastAPI app with its handler dependencies wired in.

    `provider` and `runtime` are injected so tests can swap them out.
    `session_factory`, when provided, mounts the admin router at /admin.
    `procrastinate_app`, when provided, is opened on startup + closed on
    shutdown via FastAPI's lifespan so accept_inbound's defer call has
    a live connection pool to enqueue against.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        if procrastinate_app is not None:
            async with procrastinate_app.open_async():
                yield
        else:
            yield

    app = FastAPI(title="email-assistant", lifespan=lifespan)

    if session_factory is not None:
        from email_agent.web.admin.router import mount_admin

        _protect_admin(
            app,
            username=admin_basic_auth_username,
            password=admin_basic_auth_password,
            required=admin_auth_required,
        )
        mount_admin(app, session_factory)

    @app.post("/webhooks/mailgun")
    async def mailgun_webhook(request: Request) -> Response:
        form = await request.form()
        attachment_count = sum(1 for _, v in form.multi_items() if isinstance(v, UploadFile))
        normalized_form = await _normalize_mailgun_form(form)
        webhook_request = WebhookRequest(
            headers=dict(request.headers),
            body=b"",
            form=normalized_form,
        )
        try:
            await provider.verify_webhook(webhook_request)
        except MailgunSignatureError as exc:
            log.warning(
                "signature rejected: %s | form_keys=%s",
                exc,
                sorted(webhook_request.form.keys()),
            )
            return Response(status_code=401)

        try:
            email = await provider.parse_inbound(webhook_request)
        except MailgunParseError:
            log.exception("parse failed")
            return Response(status_code=400)

        log.info(
            "inbound: %s -> %s | subject=%r | message_id=%s | in_reply_to=%s | references=%d | attachments=%d",
            email.from_email,
            email.to_emails,
            email.subject,
            email.message_id_header,
            email.in_reply_to_header,
            len(email.references_headers),
            attachment_count,
        )

        outcome = await runtime.accept_inbound(email)
        if isinstance(outcome, Dropped):
            log.warning(
                "DROPPED reason=%s detail=%s",
                outcome.reason.value,
                outcome.detail,
            )
        else:
            assert isinstance(outcome, Accepted)
            log.info(
                "ACCEPTED assistant=%s thread=%s message=%s created=%s",
                outcome.assistant_id,
                outcome.thread_id,
                outcome.message_id,
                outcome.created,
            )
        return Response(status_code=200)

    return app


def _protect_admin(
    app: FastAPI,
    *,
    username: str | None,
    password: str | None,
    required: bool,
) -> None:
    if not required and (not username or not password):
        return

    @app.middleware("http")
    async def admin_basic_auth(request: Request, call_next):
        if not request.url.path.startswith("/admin"):
            return await call_next(request)

        if not username or not password:
            return Response("Admin auth is not configured\n", status_code=503)

        supplied_username, supplied_password = _parse_basic_auth(
            request.headers.get("authorization")
        )
        if (
            supplied_username is not None
            and supplied_password is not None
            and secrets.compare_digest(supplied_username, username)
            and secrets.compare_digest(supplied_password, password)
        ):
            return await call_next(request)

        return Response(
            "Authentication required\n",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="email-assistant admin"'},
        )


def _parse_basic_auth(header: str | None) -> tuple[str | None, str | None]:
    if header is None:
        return None, None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None, None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None, None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None, None
    return username, password


async def _normalize_mailgun_form(form) -> dict[str, str]:
    """Convert a Mailgun route's multipart form into our parser's JSON shape.

    Mailgun delivers attachments as multipart files keyed `attachment-1`,
    `attachment-2`, …. We read their bytes and stuff them into the `attachments`
    form field as the inline-base64 JSON list `parse_inbound` already
    understands. String fields pass through unchanged.
    """
    string_fields: dict[str, str] = {}
    attachments: list[dict[str, str | int]] = []
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            data = await value.read()
            attachments.append(
                {
                    "filename": value.filename or key,
                    "content-type": value.content_type or "application/octet-stream",
                    "size": len(data),
                    "content": base64.b64encode(data).decode(),
                }
            )
            continue
        string_fields[key] = value
    if attachments:
        string_fields["attachments"] = json.dumps(attachments)
    return string_fields
