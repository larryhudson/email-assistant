from typing import Protocol, runtime_checkable

from email_agent.models.email import (
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)


@runtime_checkable
class EmailProvider(Protocol):
    """Boundary between the email-runtime core and any concrete provider.

    `MailgunEmailProvider` (later slice) and `InMemoryEmailProvider` (tests)
    both implement this. The core never imports concrete providers.
    """

    async def verify_webhook(self, request: WebhookRequest) -> None: ...

    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail: ...

    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail: ...
