from collections.abc import Callable

from email_agent.domain.budget_governor import BudgetLimitReply
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

    No model call. Threading headers chain off the inbound so the recipient's
    mail client groups it. Slice 5's `ReplyEnvelopeBuilder` will share the
    same subject + `References` rules.
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

    references = [*inbound.references_headers, inbound.message_id_header]

    return NormalizedOutboundEmail(
        from_email=scope.inbound_address,
        to_emails=[inbound.from_email],
        subject=_re_prefixed(inbound.subject),
        body_text=body,
        message_id_header=message_id_factory(),
        in_reply_to_header=inbound.message_id_header,
        references_headers=references,
    )


def _re_prefixed(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


__all__ = ["build_budget_limit_reply"]
