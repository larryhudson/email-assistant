import uuid
from collections import deque

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
        # Mailgun returns globally-unique IDs per send. Mirror that with a
        # uuid (not a monotonic counter), otherwise dry-run worker restarts
        # collide on `inmem-1` and RunRecorder's `(assistant_id,
        # provider_message_id)` idempotency silently returns the stale row.
        return SentEmail(
            provider_message_id=f"inmem-{uuid.uuid4().hex[:12]}",
            message_id_header=reply.message_id_header,
        )
