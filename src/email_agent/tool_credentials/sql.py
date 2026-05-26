from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import ToolCredentialRow
from email_agent.tool_credentials.port import (
    ActiveToolCredential,
    MultipleActiveToolCredentialsError,
    ToolCredentialStatus,
)


class SqlToolCredentialResolver:
    """SQL-backed `ToolCredentialResolver` over the `tool_credentials` table.

    Opens its own short-lived session per call so callers don't need to
    thread a session through host-side adapter constructors.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_active(
        self, assistant_id: str, tool_credential_key: str
    ) -> ActiveToolCredential | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ToolCredentialRow).where(
                    ToolCredentialRow.assistant_id == assistant_id,
                    ToolCredentialRow.tool_credential_key == tool_credential_key,
                    ToolCredentialRow.status == ToolCredentialStatus.ACTIVE.value,
                )
            )
            rows = result.scalars().all()
        if not rows:
            return None
        if len(rows) > 1:
            raise MultipleActiveToolCredentialsError(assistant_id, tool_credential_key, len(rows))
        row = rows[0]
        return ActiveToolCredential(
            id=row.id,
            assistant_id=row.assistant_id,
            tool_credential_key=row.tool_credential_key,
            label=row.label,
            account_identifier=row.account_identifier,
            credential_kind=row.credential_kind,
            secret_ref=row.secret_ref,
            metadata=dict(row.extra_metadata or {}),
            status=ToolCredentialStatus(row.status),
            last_verified_at=row.last_verified_at,
        )


__all__ = ["SqlToolCredentialResolver"]
