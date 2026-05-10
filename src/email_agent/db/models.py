from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from email_agent.db.base import Base


def _str_pk() -> Mapped[str]:
    return mapped_column(String(64), primary_key=True)


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[str] = _str_pk()
    name: Mapped[str] = mapped_column(String(255))
    primary_admin_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billing_scope: Mapped[str] = mapped_column(String(64), default="self")


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[str] = _str_pk()
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[str] = mapped_column(String(32), default="admin")


class EndUser(Base):
    __tablename__ = "end_users"

    id: Mapped[str] = _str_pk()
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Assistant(Base):
    __tablename__ = "assistants"

    id: Mapped[str] = _str_pk()
    end_user_id: Mapped[str] = mapped_column(ForeignKey("end_users.id"))
    inbound_address: Mapped[str] = mapped_column(String(320), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    allowed_senders: Mapped[list[str]] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64))
    system_prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    scope: Mapped["AssistantScopeRow"] = relationship(back_populates="assistant", uselist=False)


class AssistantScopeRow(Base):
    __tablename__ = "assistant_scopes"

    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"), primary_key=True)
    memory_namespace: Mapped[str] = mapped_column(String(128))
    tool_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    budget_id: Mapped[str] = mapped_column(ForeignKey("budgets.id"))

    assistant: Mapped[Assistant] = relationship(back_populates="scope")


class EmailThread(Base):
    __tablename__ = "email_threads"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    end_user_id: Mapped[str] = mapped_column(ForeignKey("end_users.id"))
    root_message_id: Mapped[str] = mapped_column(String(998))
    subject_normalized: Mapped[str] = mapped_column(String(998))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[str] = _str_pk()
    thread_id: Mapped[str] = mapped_column(ForeignKey("email_threads.id"))
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    direction: Mapped[str] = mapped_column(String(16))  # inbound | outbound
    provider_message_id: Mapped[str] = mapped_column(String(255))
    message_id_header: Mapped[str] = mapped_column(String(998))
    in_reply_to_header: Mapped[str | None] = mapped_column(String(998), nullable=True)
    references_headers: Mapped[list[str]] = mapped_column(JSON, default=list)
    from_email: Mapped[str] = mapped_column(String(320))
    to_emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(998))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("assistant_id", "provider_message_id"),)


class EmailAttachmentRow(Base):
    __tablename__ = "email_attachments"

    id: Mapped[str] = _str_pk()
    message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(String(1024))


class MessageIndex(Base):
    __tablename__ = "message_index"

    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"), primary_key=True)
    message_id_header: Mapped[str] = mapped_column(String(998), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("email_threads.id"))
    provider_message_id: Mapped[str] = mapped_column(String(255))

    __table_args__ = (UniqueConstraint("assistant_id", "message_id_header"),)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    thread_id: Mapped[str] = mapped_column(ForeignKey("email_threads.id"))
    inbound_message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id"))
    reply_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_messages.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunStep(Base):
    __tablename__ = "run_steps"

    id: Mapped[str] = _str_pk()
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    kind: Mapped[str] = mapped_column(String(32))
    input_summary: Mapped[str] = mapped_column(Text)
    output_summary: Mapped[str] = mapped_column(Text)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageLedger(Base):
    __tablename__ = "usage_ledger"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_cents: Mapped[int] = mapped_column(Integer)
    budget_period: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Budget(Base):
    __tablename__ = "budgets"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    monthly_limit_cents: Mapped[int] = mapped_column(Integer)
    period_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_resets_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = [
    "Admin",
    "AgentRun",
    "Assistant",
    "AssistantScopeRow",
    "Base",
    "Budget",
    "EmailAttachmentRow",
    "EmailMessage",
    "EmailThread",
    "EndUser",
    "MessageIndex",
    "Owner",
    "RunStep",
    "UsageLedger",
]
