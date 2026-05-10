"""Cognee-backed MemoryPort adapter.

Maps our domain identifiers onto cognee 1.0's tenant model:

- our `assistant_id` → a cognee `User` (the tenant boundary). Each
  assistant gets its own User; cognee's authorization keeps memory
  invisible to other users via `user=...` on remember/recall/forget.
- our `thread_id` → cognee `session_id` (the conversation cache,
  with auto-bridging to the user's durable graph via improve()).

This avoids the per-assistant `data_root_directory` swapping that
fought cognee's intended model. One shared cognee installation, one
relational DB, one set of vector/graph stores; tenants are separated
at the User level.

The agent never gets a write-side memory tool — durable memory writes
happen out-of-band after the run completes (curate_memory + cognee
session traces), where the curation step can see the whole turn.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING, Any

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError
from cognee.modules.search.types import SearchType
from cognee.modules.users.methods import (
    create_user,
    delete_user,
    get_user_by_email,
)

from email_agent.models.memory import Memory, MemoryContext

if TYPE_CHECKING:
    from cognee.modules.users.models import User


class CogneeMemoryAdapter:
    """`MemoryPort` adapter backed by cognee.remember / cognee.recall.

    One shared cognee installation; tenancy via cognee `User` objects
    (one per `assistant_id`). Users are lazily created on first touch
    and cached per-process. The synthetic email scheme maps deterministically
    so a restarted process re-resolves to the same user.
    """

    def __init__(self) -> None:
        self._counter = count(1)
        self._users: dict[str, User] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._setup_lock = asyncio.Lock()
        self._setup_done = False

    async def _ensure_setup(self) -> None:
        """Run cognee.setup() once per process. Creates the relational +
        vector tables that `get_user_by_email` and `create_user` need to
        exist before any user-management call works. Idempotent in cognee
        — repeat calls are cheap once tables exist."""
        if self._setup_done:
            return
        async with self._setup_lock:
            if self._setup_done:
                return
            from cognee.modules.engine.operations.setup import setup

            await setup()
            self._setup_done = True

    async def record_turn(self, assistant_id: str, thread_id: str, role: str, content: str) -> None:
        # We deliberately don't pass session_id. Cognee's session-memory
        # write path creates a `datasets` row but skips the ACL grants
        # that `create_authorized_dataset` makes — so the subsequent
        # `improve()` bridge fails with "User does not have write access".
        # Writing straight to the durable graph triggers proper dataset
        # creation + permissions, and recall (which still passes
        # session_id) falls through to the graph anyway. We keep the
        # thread_id in the prefix so the agent can distinguish turns
        # from different threads in retrieved chunks.
        user = await self._user_for(assistant_id)
        await cognee.remember(f"[thread:{thread_id} role:{role}] {content}", user=user)

    async def recall(self, assistant_id: str, thread_id: str, query: str) -> MemoryContext:
        user = await self._user_for(assistant_id)
        # query_type=CHUNKS + only_context=True returns raw retrieved chunks
        # rather than cognee's LLM-synthesized answer. NoDataError fires on
        # a fresh user with no ingested chunks yet — that's "no memories",
        # not a failure.
        try:
            results = await cognee.recall(
                query,
                session_id=thread_id,
                query_type=SearchType.CHUNKS,
                only_context=True,
                user=user,
            )
        except NoDataError:
            results = []
        return MemoryContext(
            memories=[self._to_memory(r) for r in results],
            retrieved_at=datetime.now(UTC),
        )

    async def search(self, assistant_id: str, query: str) -> list[Memory]:
        user = await self._user_for(assistant_id)
        try:
            results = await cognee.recall(
                query,
                query_type=SearchType.CHUNKS,
                only_context=True,
                user=user,
            )
        except NoDataError:
            results = []
        return [self._to_memory(r) for r in results]

    async def seed_durable(self, assistant_id: str, content: str) -> None:
        """Seed a fact directly into the assistant's durable graph
        (no session_id). Ops affordance for the `seed-memory` CLI."""
        user = await self._user_for(assistant_id)
        await cognee.remember(content, user=user)

    async def delete_assistant(self, assistant_id: str) -> None:
        email = self._assistant_email(assistant_id)
        user = self._users.pop(assistant_id, None)
        if user is None:
            user = await get_user_by_email(email)
        if user is None:
            return
        # Drops every dataset + graph the user owns.
        await cognee.forget(everything=True, user=user)
        await delete_user(email)

    async def _user_for(self, assistant_id: str) -> User:
        cached = self._users.get(assistant_id)
        if cached is not None:
            return cached
        await self._ensure_setup()
        # Per-assistant lock so concurrent first-touches for the same
        # assistant don't race two create_user calls.
        lock = self._user_locks.setdefault(assistant_id, asyncio.Lock())
        async with lock:
            cached = self._users.get(assistant_id)
            if cached is not None:
                return cached
            email = self._assistant_email(assistant_id)
            user = await get_user_by_email(email)
            if user is None:
                user = await create_user(
                    email=email,
                    password=_synthetic_password(assistant_id),
                    is_verified=True,
                )
            self._users[assistant_id] = user
            return user

    @staticmethod
    def _assistant_email(assistant_id: str) -> str:
        # `example.com` is the RFC 2606 reserved-for-documentation domain.
        # Cognee's email validator (pydantic EmailStr → email_validator)
        # rejects "special-use" TLDs like .local / .invalid / .test, but
        # accepts example.com because it's a regular .com.
        return f"assistant-{assistant_id}@example.com"

    def _to_memory(self, raw: dict[str, Any]) -> Memory:
        # cognee.recall(query_type=CHUNKS, only_context=True) returns dicts of
        # shape {dataset_id, dataset_name, dataset_tenant_id, search_result, _source}.
        # `search_result` is the raw chunk text we want to inject into the prompt.
        return Memory(id=f"mem-{next(self._counter)}", content=raw["search_result"])


def _synthetic_password(assistant_id: str) -> str:
    """Stable per-assistant password. We never use it to log in (the
    backend takes the User object directly), but cognee's user-creation
    flow requires one. Derived deterministically so re-runs produce the
    same value, but it's never exposed externally."""
    return f"email-agent::{assistant_id}::synthetic"
