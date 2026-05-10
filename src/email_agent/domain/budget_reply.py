from collections.abc import Callable

from email_agent.domain.budget_governor import BudgetLimitReply
from email_agent.domain.reply_envelope import ReplyEnvelopeBuilder
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import NormalizedInboundEmail, NormalizedOutboundEmail


def build_budget_limit_reply(
    *,
    inbound: NormalizedInboundEmail,
    scope: AssistantScope,
    decision: BudgetLimitReply,
    message_id_factory: Callable[[], str],
) -> NormalizedOutboundEmail:
    """Build the cheap canned reply sent when an assistant hits its monthly cap.

    No model call. Threading headers chain off the inbound via
    `ReplyEnvelopeBuilder`; only the body is custom here.
    """
    days = decision.days_until_reset
    day_word = "day" if days == 1 else "days"
    body = (
        f"Hi,\n\n"
        f"Thanks for your message. This assistant has reached its monthly "
        f"budget and won't be able to reply to anything else for the next "
        f"{days} {day_word}.\n\n"
        f"It will be back automatically when the next billing period starts.\n"
    )

    return ReplyEnvelopeBuilder().build(
        inbound=inbound,
        from_email=scope.inbound_address,
        body_text=body,
        attachments=[],
        message_id_factory=message_id_factory,
    )


__all__ = ["build_budget_limit_reply"]
