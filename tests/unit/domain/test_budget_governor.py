from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    Assistant,
    AssistantScopeRow,
    Budget,
    EndUser,
    Owner,
    UsageLedger,
)
from email_agent.domain.budget_governor import Allow, BudgetGovernor, BudgetLimitReply
from email_agent.models.assistant import AssistantScope, AssistantStatus


async def _seed(
    session: AsyncSession,
    *,
    monthly_limit_usd: Decimal,
    spent_usd: Decimal,
    spent_at: datetime,
    period_starts_at: datetime = datetime(2026, 5, 1, tzinfo=UTC),
    period_resets_at: datetime = datetime(2026, 6, 1, tzinfo=UTC),
) -> None:
    session.add(Owner(id="o-1", name="Larry"))
    session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
    session.add(
        Budget(
            id="b-1",
            assistant_id="a-1",
            monthly_limit_usd=monthly_limit_usd,
            period_starts_at=period_starts_at,
            period_resets_at=period_resets_at,
        )
    )
    session.add(
        Assistant(
            id="a-1",
            end_user_id="u-1",
            inbound_address="mum@assistants.example.com",
            status="active",
            allowed_senders=["mum@example.com"],
            model="deepseek-flash",
            system_prompt="be kind",
        )
    )
    session.add(
        AssistantScopeRow(
            assistant_id="a-1",
            memory_namespace="mum",
            tool_allowlist=["read"],
            budget_id="b-1",
        )
    )
    session.add(
        UsageLedger(
            id="l-1",
            assistant_id="a-1",
            run_id="r-1",
            provider="deepseek",
            model="deepseek-flash",
            input_tokens=0,
            output_tokens=0,
            cost_usd=spent_usd,
            budget_period="2026-05",
            created_at=spent_at,
        )
    )
    await session.commit()


def _scope() -> AssistantScope:
    return AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        end_user_id="u-1",
        inbound_address="mum@assistants.example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="mum",
        tool_allowlist=("read",),
        budget_id="b-1",
        model_name="deepseek-flash",
        system_prompt="be kind",
    )


async def test_governor_allows_when_under_limit(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed(
            session,
            monthly_limit_usd=Decimal("10.00"),
            spent_usd=Decimal("1.00"),
            spent_at=datetime(2026, 5, 5, tzinfo=UTC),
        )

    governor = BudgetGovernor(sqlite_session_factory)
    decision = await governor.decide(_scope())

    assert isinstance(decision, Allow)


async def test_governor_blocks_when_at_limit(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed(
            session,
            monthly_limit_usd=Decimal("10.00"),
            spent_usd=Decimal("10.00"),
            spent_at=datetime(2026, 5, 5, tzinfo=UTC),
        )

    now = datetime(2026, 5, 29, tzinfo=UTC)
    governor = BudgetGovernor(sqlite_session_factory, now=lambda: now)
    decision = await governor.decide(_scope())

    assert decision == BudgetLimitReply(
        monthly_limit_usd=Decimal("10.00"),
        spent_usd=Decimal("10.00"),
        days_until_reset=3,
    )


async def test_governor_blocks_when_over_limit(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed(
            session,
            monthly_limit_usd=Decimal("10.00"),
            spent_usd=Decimal("15.00"),
            spent_at=datetime(2026, 5, 5, tzinfo=UTC),
        )

    now = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)
    governor = BudgetGovernor(sqlite_session_factory, now=lambda: now)
    decision = await governor.decide(_scope())

    assert decision == BudgetLimitReply(
        monthly_limit_usd=Decimal("10.00"),
        spent_usd=Decimal("15.00"),
        days_until_reset=1,
    )


async def test_governor_ignores_ledger_rows_outside_active_period(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    # An old, expensive run from a prior period plus a small in-period run.
    # Total exceeds the cap, but only the in-period row counts → still under.
    async with sqlite_session_factory() as session:
        session.add(Owner(id="o-1", name="Larry"))
        session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
        session.add(
            Budget(
                id="b-1",
                assistant_id="a-1",
                monthly_limit_usd=Decimal("10.00"),
                period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
                period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        session.add(
            Assistant(
                id="a-1",
                end_user_id="u-1",
                inbound_address="mum@assistants.example.com",
                status="active",
                allowed_senders=["mum@example.com"],
                model="deepseek-flash",
                system_prompt="be kind",
            )
        )
        session.add(
            AssistantScopeRow(
                assistant_id="a-1",
                memory_namespace="mum",
                tool_allowlist=["read"],
                budget_id="b-1",
            )
        )
        session.add(
            UsageLedger(
                id="l-old",
                assistant_id="a-1",
                run_id="r-old",
                provider="deepseek",
                model="deepseek-flash",
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("50.00"),
                budget_period="2026-04",
                created_at=datetime(2026, 4, 15, tzinfo=UTC),
            )
        )
        session.add(
            UsageLedger(
                id="l-new",
                assistant_id="a-1",
                run_id="r-new",
                provider="deepseek",
                model="deepseek-flash",
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("2.00"),
                budget_period="2026-05",
                created_at=datetime(2026, 5, 5, tzinfo=UTC),
            )
        )
        await session.commit()

    governor = BudgetGovernor(sqlite_session_factory)
    decision = await governor.decide(_scope())

    assert isinstance(decision, Allow)
