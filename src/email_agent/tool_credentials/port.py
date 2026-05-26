from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class ToolCredentialStatus(StrEnum):
    """Operational state of a stored tool credential.

    `active` is the only state the resolver returns. `revoked` and `error`
    are preserved so admins can keep history and last-error context without
    re-enabling stale rows.
    """

    ACTIVE = "active"
    REVOKED = "revoked"
    ERROR = "error"


class ActiveToolCredential(BaseModel):
    """Resolved view of a single active credential row.

    Carries `secret_ref` (an opaque reference, NOT a secret) and the
    non-secret `metadata` mapping. Adapters interpret `credential_kind` to
    decide how to dereference `secret_ref` — e.g. ``file:...`` for the MVP
    Google Workspace flow.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    assistant_id: str
    tool_credential_key: str
    label: str
    account_identifier: str | None
    credential_kind: str
    secret_ref: str
    metadata: dict[str, Any]
    status: ToolCredentialStatus
    last_verified_at: datetime | None


class MultipleActiveToolCredentialsError(RuntimeError):
    """More than one `active` row for the same (assistant, key).

    The MVP invariant is one active credential per (assistant_id,
    tool_credential_key). We enforce this in the resolver rather than via
    a partial unique index (SQLite/Postgres compatibility); seeing this
    error means a writer skipped the invariant.
    """

    def __init__(self, assistant_id: str, tool_credential_key: str, count: int) -> None:
        super().__init__(
            f"Found {count} active tool_credentials for assistant_id={assistant_id!r} "
            f"tool_credential_key={tool_credential_key!r}; MVP allows at most one."
        )
        self.assistant_id = assistant_id
        self.tool_credential_key = tool_credential_key
        self.count = count


class ToolCredentialResolver(Protocol):
    """Resolve the active host-side credential for an assistant + tool key.

    Returns `None` when no active credential is linked. Never exposes raw
    secret material; adapters take `secret_ref` and dereference it in their
    own trusted context.
    """

    async def get_active(
        self, assistant_id: str, tool_credential_key: str
    ) -> ActiveToolCredential | None: ...


__all__ = [
    "ActiveToolCredential",
    "MultipleActiveToolCredentialsError",
    "ToolCredentialResolver",
    "ToolCredentialStatus",
]
