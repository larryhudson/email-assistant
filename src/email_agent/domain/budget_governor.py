from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.functions import coalesce
from sqlalchemy.sql.functions import sum as sql_sum

from email_agent.db.models import Budget, UsageLedger
from email_agent.models.assistant import AssistantScope


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


class BudgetGovernor:
    """Reads `usage_ledger` for the active period and decides whether to run.

    Sits in front of every agent run (slice 5 wires this in). The decision is
    a pure function of the ledger sum vs `Budget.monthly_limit_cents`; this
    class owns the SQL.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def decide(self, scope: AssistantScope) -> BudgetDecision:
        async with self._session_factory() as session:
            budget = await session.get(Budget, scope.budget_id)
            if budget is None:
                raise LookupError(f"budget {scope.budget_id} not found")
            spent_stmt = select(coalesce(sql_sum(UsageLedger.cost_cents), 0)).where(
                UsageLedger.assistant_id == scope.assistant_id,
                UsageLedger.created_at >= budget.period_starts_at,
                UsageLedger.created_at < budget.period_resets_at,
            )
            spent = (await session.execute(spent_stmt)).scalar_one()

        if spent < budget.monthly_limit_cents:
            return Allow()
        # Tasks 2-3 will return BudgetLimitReply here.
        raise NotImplementedError("at/over-limit branch lands in task 2")


__all__ = ["Allow", "BudgetDecision", "BudgetGovernor", "BudgetLimitReply"]
