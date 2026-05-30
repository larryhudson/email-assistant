from sqlalchemy import UniqueConstraint

from email_agent.db.models import Base, ToolCredentialRow


def test_expected_tables_are_registered():
    expected = {
        "owners",
        "admins",
        "end_users",
        "assistants",
        "assistant_scopes",
        "assistant_surfaces",
        "email_threads",
        "email_messages",
        "email_attachments",
        "message_index",
        "agent_runs",
        "run_steps",
        "usage_ledger",
        "budgets",
        "tool_credentials",
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


def test_tool_credentials_column_shape():
    t = Base.metadata.tables["tool_credentials"]
    columns = {c.name for c in t.columns}
    # `metadata` is the DB column name even though the Python attribute on
    # the model is `extra_metadata` (DeclarativeBase reserves `metadata`).
    assert {
        "id",
        "assistant_id",
        "tool_credential_key",
        "label",
        "account_identifier",
        "credential_kind",
        "secret_ref",
        "metadata",
        "status",
        "last_verified_at",
        "last_error",
        "created_at",
        "updated_at",
    } <= columns


def test_assistant_surfaces_column_shape():
    t = Base.metadata.tables["assistant_surfaces"]
    columns = {c.name for c in t.columns}
    assert {
        "assistant_id",
        "enabled",
        "port",
        "created_at",
        "updated_at",
    } <= columns


def test_tool_credential_row_metadata_attribute_is_renamed():
    # Constructing the ORM model uses `extra_metadata`; the DB column is
    # still named `metadata`. Documents the rename for future readers.
    row = ToolCredentialRow(
        id="tc-1",
        assistant_id="a-1",
        tool_credential_key="google_workspace",
        label="L",
        credential_kind="google_authorized_user_file",
        secret_ref="file:/tmp/x.json",
        extra_metadata={"scopes": ["calendar"]},
        status="active",
    )
    assert row.extra_metadata == {"scopes": ["calendar"]}
