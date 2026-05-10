import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

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
    and when service resumes. Amounts are in USD as Decimal (4dp).
    """

    monthly_limit_usd: Decimal
    spent_usd: Decimal
    days_until_reset: int


BudgetDecision = Allow | BudgetLimitReply


class BudgetGovernor:
    """Reads `usage_ledger` for the active period and decides whether to run.

    Sits in front of every agent run. The decision is a pure function of the
    ledger sum vs `Budget.monthly_limit_usd`; this class owns the SQL.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._now = now

    async def decide(self, scope: AssistantScope) -> BudgetDecision:
        async with self._session_factory() as session:
            budget = await session.get(Budget, scope.budget_id)
            if budget is None:
                raise LookupError(f"budget {scope.budget_id} not found")
            spent_stmt = select(coalesce(sql_sum(UsageLedger.cost_usd), Decimal("0"))).where(
                UsageLedger.assistant_id == scope.assistant_id,
                UsageLedger.created_at >= budget.period_starts_at,
                UsageLedger.created_at < budget.period_resets_at,
            )
            spent_raw = (await session.execute(spent_stmt)).scalar_one()
            spent: Decimal = (
                spent_raw if isinstance(spent_raw, Decimal) else Decimal(str(spent_raw))
            )

        if spent < budget.monthly_limit_usd:
            return Allow()

        period_resets_at = budget.period_resets_at
        if period_resets_at.tzinfo is None:
            # sqlite (test backend) drops tzinfo; postgres preserves it.
            period_resets_at = period_resets_at.replace(tzinfo=UTC)
        remaining = period_resets_at - self._now()
        days_until_reset = max(0, math.ceil(remaining.total_seconds() / 86400))
        return BudgetLimitReply(
            monthly_limit_usd=budget.monthly_limit_usd,
            spent_usd=spent,
            days_until_reset=days_until_reset,
        )


__all__ = ["Allow", "BudgetDecision", "BudgetGovernor", "BudgetLimitReply"]
