from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import Assistant, AssistantSurfaceRow


async def set_assistant_surface(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assistant_id: str,
    enabled: bool,
    port: int | None = None,
) -> AssistantSurfaceRow | None:
    async with session_factory() as session:
        assistant = await session.get(Assistant, assistant_id)
        if assistant is None:
            return None
        row = await session.get(AssistantSurfaceRow, assistant_id)
        if row is None:
            row = AssistantSurfaceRow(assistant_id=assistant_id)
            session.add(row)
        row.enabled = enabled
        if port is not None:
            row.port = port
        await session.commit()
        return row


__all__ = ["set_assistant_surface"]
