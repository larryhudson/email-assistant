import os
import uuid

import pytest

from email_agent.config import Settings
from email_agent.db.models import Assistant, EndUser, Owner
from email_agent.db.session import make_engine, make_session_factory, session_scope

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs db")
async def test_insert_and_query_assistant():
    engine = make_engine(Settings())  # ty: ignore[missing-argument]
    factory = make_session_factory(engine)

    owner_id = f"o-{uuid.uuid4().hex[:8]}"
    user_id = f"u-{uuid.uuid4().hex[:8]}"
    asst_id = f"a-{uuid.uuid4().hex[:8]}"

    async with session_scope(factory) as s:
        s.add(Owner(id=owner_id, name="Larry"))
        await s.flush()
        s.add(EndUser(id=user_id, owner_id=owner_id, email=f"{user_id}@example.com"))
        await s.flush()
        s.add(
            Assistant(
                id=asst_id,
                end_user_id=user_id,
                inbound_address=f"{asst_id}@example.com",
                model="deepseek-flash",
                system_prompt="be kind",
            )
        )

    async with session_scope(factory) as s:
        got = await s.get(Assistant, asst_id)
        assert got is not None
        assert got.inbound_address == f"{asst_id}@example.com"

    await engine.dispose()
