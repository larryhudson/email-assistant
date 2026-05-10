from collections import deque
from itertools import count

from email_agent.models.email import (
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)


class InMemoryEmailProvider:
    """In-process EmailProvider for tests.

    `queue_inbound(...)` enqueues emails that subsequent `parse_inbound` calls
    will return in FIFO order. `sent` records every reply for assertion.
    """

    def __init__(self, *, verify_should_raise: Exception | None = None) -> None:
        self._inbox: deque[NormalizedInboundEmail] = deque()
        self.sent: list[NormalizedOutboundEmail] = []
        self._verify_should_raise = verify_should_raise
        self._counter = count(1)

    def queue_inbound(self, email: NormalizedInboundEmail) -> None:
        self._inbox.append(email)

    async def verify_webhook(self, request: WebhookRequest) -> None:
        if self._verify_should_raise is not None:
            raise self._verify_should_raise

    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail:
        if not self._inbox:
            raise LookupError("no inbound emails queued")
        return self._inbox.popleft()

    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail:
        self.sent.append(reply)
        return SentEmail(
            provider_message_id=f"inmem-{next(self._counter)}",
            message_id_header=reply.message_id_header,
        )
