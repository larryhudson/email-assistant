from datetime import UTC, datetime
from decimal import Decimal

from email_agent.domain.budget_governor import BudgetLimitReply
from email_agent.domain.budget_reply import build_budget_limit_reply
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import NormalizedInboundEmail


def _scope() -> AssistantScope:
    return AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        owner_email="owner@example.com",
        end_user_id="u-1",
        end_user_email="mum@example.com",
        inbound_address="mum@assistants.example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="mum",
        tool_allowlist=("read",),
        budget_id="b-1",
        model_name="deepseek-flash",
        system_prompt="be kind",
    )


def _inbound(*, subject: str = "Question?") -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id="prov-1",
        message_id_header="<m1@x>",
        in_reply_to_header=None,
        references_headers=["<r0@x>"],
        from_email="mum@example.com",
        to_emails=["mum@assistants.example.com"],
        subject=subject,
        body_text="hello",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


def test_builds_threading_correct_envelope_with_re_prefix():
    reply = build_budget_limit_reply(
        inbound=_inbound(),
        scope=_scope(),
        decision=BudgetLimitReply(
            monthly_limit_usd=Decimal("10.00"), spent_usd=Decimal("10.00"), days_until_reset=3
        ),
        message_id_factory=lambda: "<run-abc@assistants.example.com>",
    )

    assert reply.from_email == "mum@assistants.example.com"
    assert reply.to_emails == ["mum@example.com"]
    assert reply.subject == "Re: Question?"
    assert reply.in_reply_to_header == "<m1@x>"
    assert reply.references_headers == ["<r0@x>", "<m1@x>"]
    assert reply.message_id_header == "<run-abc@assistants.example.com>"
    assert "monthly budget" in reply.body_text.lower()
    assert "3 days" in reply.body_text


def test_does_not_double_prefix_re():
    reply = build_budget_limit_reply(
        inbound=_inbound(subject="re: already replying"),
        scope=_scope(),
        decision=BudgetLimitReply(
            monthly_limit_usd=Decimal("10.00"), spent_usd=Decimal("10.00"), days_until_reset=1
        ),
        message_id_factory=lambda: "<run-abc@x>",
    )

    assert reply.subject == "re: already replying"


def test_singular_day_when_one_day_left():
    reply = build_budget_limit_reply(
        inbound=_inbound(),
        scope=_scope(),
        decision=BudgetLimitReply(
            monthly_limit_usd=Decimal("10.00"), spent_usd=Decimal("10.00"), days_until_reset=1
        ),
        message_id_factory=lambda: "<run-abc@x>",
    )

    assert "1 day" in reply.body_text
    assert "1 days" not in reply.body_text
