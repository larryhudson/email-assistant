import logging

from fastapi import FastAPI, Request, Response

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

log = logging.getLogger(__name__)


def build_app_from_settings() -> FastAPI:
    """Compose the production app from environment-backed settings."""
    from email_agent.config import Settings
    from email_agent.db.session import make_engine, make_session_factory

    settings = Settings()  # ty: ignore[missing-argument]
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    provider = MailgunEmailProvider(
        signing_key=settings.mailgun_signing_key.get_secret_value(),
    )
    runtime = AssistantRuntime(factory, attachments_root=settings.attachments_root)
    settings.attachments_root.mkdir(parents=True, exist_ok=True)
    return build_app(provider=provider, runtime=runtime)


def build_app(*, provider: MailgunEmailProvider, runtime: AssistantRuntime) -> FastAPI:
    """Build the FastAPI app with its handler dependencies wired in.

    `provider` and `runtime` are injected so tests can swap them out.
    """
    app = FastAPI(title="email-assistant")

    @app.post("/webhooks/mailgun")
    async def mailgun_webhook(request: Request) -> Response:
        form = await request.form()
        webhook_request = WebhookRequest(
            headers=dict(request.headers),
            body=b"",
            form={k: v for k, v in form.items() if isinstance(v, str)},
        )
        try:
            await provider.verify_webhook(webhook_request)
        except MailgunSignatureError:
            log.warning("mailgun webhook signature rejected")
            return Response(status_code=401)

        try:
            email = await provider.parse_inbound(webhook_request)
        except MailgunParseError:
            log.exception("mailgun webhook parse failed")
            return Response(status_code=400)

        outcome = await runtime.accept_inbound(email)
        if isinstance(outcome, Dropped):
            log.info(
                "inbound dropped",
                extra={"reason": outcome.reason.value, "detail": outcome.detail},
            )
        else:
            assert isinstance(outcome, Accepted)
            log.info(
                "inbound accepted",
                extra={
                    "assistant_id": outcome.assistant_id,
                    "thread_id": outcome.thread_id,
                    "message_id": outcome.message_id,
                    "created": outcome.created,
                },
            )
        return Response(status_code=200)

    return app
