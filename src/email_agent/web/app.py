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
