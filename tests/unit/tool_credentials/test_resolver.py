import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import Assistant, EndUser, Owner, ToolCredentialRow
from email_agent.tool_credentials import (
    MultipleActiveToolCredentialsError,
    SqlToolCredentialResolver,
    ToolCredentialStatus,
)

pytestmark = pytest.mark.asyncio


async def _seed_assistant(
    session_factory: async_sessionmaker[AsyncSession],
    assistant_id: str = "a-1",
) -> None:
    suffix = assistant_id
    async with session_factory() as s:
        s.add(Owner(id=f"o-{suffix}", name="O"))
        await s.flush()
        s.add(
            EndUser(
                id=f"u-{suffix}",
                owner_id=f"o-{suffix}",
                email=f"{assistant_id}@example.com",
            )
        )
        await s.flush()
        s.add(
            Assistant(
                id=assistant_id,
                end_user_id=f"u-{suffix}",
                inbound_address=f"{assistant_id}@assistants.example.com",
                model="m",
            )
        )
        await s.commit()


def _row(
    *,
    id: str = "tc-1",
    assistant_id: str = "a-1",
    tool_credential_key: str = "google_workspace",
    label: str = "Larry GW",
    account_identifier: str | None = "larry@example.com",
    credential_kind: str = "google_authorized_user_file",
    secret_ref: str = "file:data/tool_credentials/a-1/gw/credentials.json",
    extra_metadata: dict | None = None,
    status: str = "active",
) -> ToolCredentialRow:
    return ToolCredentialRow(
        id=id,
        assistant_id=assistant_id,
        tool_credential_key=tool_credential_key,
        label=label,
        account_identifier=account_identifier,
        credential_kind=credential_kind,
        secret_ref=secret_ref,
        extra_metadata=extra_metadata if extra_metadata is not None else {"scopes": ["calendar"]},
        status=status,
    )


async def test_get_active_returns_none_when_missing(sqlite_session_factory):
    await _seed_assistant(sqlite_session_factory)
    resolver = SqlToolCredentialResolver(sqlite_session_factory)

    got = await resolver.get_active("a-1", "google_workspace")
    assert got is None


async def test_get_active_returns_safe_view_without_secret(sqlite_session_factory):
    await _seed_assistant(sqlite_session_factory)
    async with sqlite_session_factory() as s:
        s.add(_row())
        await s.commit()

    resolver = SqlToolCredentialResolver(sqlite_session_factory)
    got = await resolver.get_active("a-1", "google_workspace")

    assert got is not None
    assert got.id == "tc-1"
    assert got.tool_credential_key == "google_workspace"
    assert got.account_identifier == "larry@example.com"
    assert got.credential_kind == "google_authorized_user_file"
    assert got.secret_ref.startswith("file:")
    assert got.metadata == {"scopes": ["calendar"]}
    assert got.status is ToolCredentialStatus.ACTIVE
    # Resolver model exposes secret_ref (opaque) and metadata only — no
    # `credential` / `secret` field that could hold raw material.
    assert not any(
        f in type(got).model_fields for f in ("credential", "secret", "token", "refresh_token")
    )


async def test_get_active_ignores_revoked_and_error_rows(sqlite_session_factory):
    await _seed_assistant(sqlite_session_factory)
    async with sqlite_session_factory() as s:
        s.add(_row(id="tc-r", status="revoked"))
        s.add(_row(id="tc-e", status="error"))
        await s.commit()

    resolver = SqlToolCredentialResolver(sqlite_session_factory)
    assert await resolver.get_active("a-1", "google_workspace") is None


async def test_get_active_filters_by_assistant_and_key(sqlite_session_factory):
    await _seed_assistant(sqlite_session_factory, assistant_id="a-1")
    await _seed_assistant(sqlite_session_factory, assistant_id="a-2")
    async with sqlite_session_factory() as s:
        s.add(_row(id="tc-a1", assistant_id="a-1", tool_credential_key="google_workspace"))
        s.add(_row(id="tc-a1-gh", assistant_id="a-1", tool_credential_key="github"))
        s.add(_row(id="tc-a2", assistant_id="a-2", tool_credential_key="google_workspace"))
        await s.commit()

    resolver = SqlToolCredentialResolver(sqlite_session_factory)
    got = await resolver.get_active("a-1", "google_workspace")
    assert got is not None
    assert got.id == "tc-a1"


async def test_get_active_raises_when_multiple_active(sqlite_session_factory):
    await _seed_assistant(sqlite_session_factory)
    async with sqlite_session_factory() as s:
        s.add(_row(id="tc-a", status="active"))
        s.add(_row(id="tc-b", status="active"))
        await s.commit()

    resolver = SqlToolCredentialResolver(sqlite_session_factory)
    with pytest.raises(MultipleActiveToolCredentialsError) as exc:
        await resolver.get_active("a-1", "google_workspace")
    assert exc.value.count == 2
