import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import SurfaceTokenRow

SURFACE_TOKEN_PREFIX = "st_"


@dataclass(frozen=True)
class CreatedSurfaceToken:
    id: str
    token: str


def generate_surface_token() -> str:
    return f"{SURFACE_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def hash_surface_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_surface_token(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assistant_id: str,
) -> CreatedSurfaceToken:
    token = generate_surface_token()
    token_hash = hash_surface_token(token)
    token_id = f"st-{uuid.uuid4().hex[:12]}"
    async with session_factory() as session:
        await revoke_surface_tokens(session, assistant_id=assistant_id)
        session.add(
            SurfaceTokenRow(
                id=token_id,
                assistant_id=assistant_id,
                token_hash=token_hash,
            )
        )
        await session.commit()
    return CreatedSurfaceToken(id=token_id, token=token)


async def revoke_surface_tokens(
    session: AsyncSession,
    *,
    assistant_id: str,
) -> int:
    rows = (
        (
            await session.execute(
                select(SurfaceTokenRow).where(
                    SurfaceTokenRow.assistant_id == assistant_id,
                    SurfaceTokenRow.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
    return len(rows)


async def revoke_surface_tokens_for_assistant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assistant_id: str,
) -> int:
    async with session_factory() as session:
        count = await revoke_surface_tokens(session, assistant_id=assistant_id)
        await session.commit()
    return count


async def verify_surface_token(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assistant_id: str,
    token: str,
) -> bool:
    candidate_hash = hash_surface_token(token)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(SurfaceTokenRow.token_hash).where(
                        SurfaceTokenRow.assistant_id == assistant_id,
                        SurfaceTokenRow.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    return any(secrets.compare_digest(candidate_hash, stored_hash) for stored_hash in rows)


__all__ = [
    "CreatedSurfaceToken",
    "create_surface_token",
    "generate_surface_token",
    "hash_surface_token",
    "revoke_surface_tokens_for_assistant",
    "verify_surface_token",
]
