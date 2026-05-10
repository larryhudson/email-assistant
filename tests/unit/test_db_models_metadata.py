from sqlalchemy import UniqueConstraint

from email_agent.db.models import Base


def test_expected_tables_are_registered():
    expected = {
        "owners",
        "admins",
        "end_users",
        "assistants",
        "assistant_scopes",
        "email_threads",
        "email_messages",
        "email_attachments",
        "message_index",
        "agent_runs",
        "run_steps",
        "usage_ledger",
        "budgets",
    }
    assert expected.issubset(set(Base.metadata.tables.keys()))


def test_message_index_has_assistant_scope_unique():
    t = Base.metadata.tables["message_index"]
    uniques = {
        tuple(sorted(c.name for c in u.columns))
        for u in t.constraints
        if isinstance(u, UniqueConstraint)
    }
    assert ("assistant_id", "message_id_header") in uniques
