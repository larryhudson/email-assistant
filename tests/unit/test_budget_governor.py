from email_agent.domain.budget_governor import Allow, BudgetDecision, BudgetLimitReply


def test_allow_constructs() -> None:
    decision: BudgetDecision = Allow()
    assert isinstance(decision, Allow)


def test_budget_limit_reply_fields() -> None:
    decision = BudgetLimitReply(
        monthly_limit_cents=1000,
        spent_cents=1000,
        days_until_reset=3,
    )
    assert decision.monthly_limit_cents == 1000
    assert decision.spent_cents == 1000
    assert decision.days_until_reset == 3
