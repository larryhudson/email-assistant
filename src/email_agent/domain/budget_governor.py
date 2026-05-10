from dataclasses import dataclass


@dataclass(frozen=True)
class Allow:
    """The assistant is under its monthly cap; the run may proceed."""


@dataclass(frozen=True)
class BudgetLimitReply:
    """The assistant has hit its monthly cap; send a cheap template reply.

    Carries the numbers the template needs to tell the sender what happened
    and when service resumes.
    """

    monthly_limit_cents: int
    spent_cents: int
    days_until_reset: int


BudgetDecision = Allow | BudgetLimitReply


__all__ = ["Allow", "BudgetDecision", "BudgetLimitReply"]
