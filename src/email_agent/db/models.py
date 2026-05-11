from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from email_agent.db.base import Base

# 10 digits, 4 after the decimal — enough for $999,999.9999 / row.
USD_NUMERIC = Numeric(10, 4)


def _str_pk() -> Mapped[str]:
    return mapped_column(String(64), primary_key=True)


class Owner(Base):
    """Top-level billing/admin tenant. DB-only — no wire counterpart."""

    __tablename__ = "owners"

    id: Mapped[str] = _str_pk()
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320), default="", server_default="")
    primary_admin_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billing_scope: Mapped[str] = mapped_column(String(64), default="self")


class Admin(Base):
    """Operator who can log in and inspect runs. DB-only — no wire counterpart."""

    __tablename__ = "admins"

    id: Mapped[str] = _str_pk()
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[str] = mapped_column(String(32), default="admin")


class EndUser(Base):
    """The person on the other end of an assistant's emails. DB-only."""

    __tablename__ = "end_users"

    id: Mapped[str] = _str_pk()
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Assistant(Base):
    """Persisted shape of an assistant.

    Flattens with `AssistantScopeRow` + `Budget` into `models.assistant.AssistantScope`
    via the mapper in `domain/router.py` (lands in a later slice).
    """

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
    """Per-assistant runtime scope (memory namespace, tool allowlist, budget link).

    Joined with `Assistant` + `Budget` to produce `models.assistant.AssistantScope`
    at the start of every run. `Row` suffix disambiguates from the wire type.
    """

    __tablename__ = "assistant_scopes"

    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"), primary_key=True)
    memory_namespace: Mapped[str] = mapped_column(String(128))
    tool_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    budget_id: Mapped[str] = mapped_column(ForeignKey("budgets.id"))

    assistant: Mapped[Assistant] = relationship(back_populates="scope")


class EmailThread(Base):
    """A conversation an assistant has with an end user.

    DB-only — threads are inferred from headers by `domain/thread_resolver.py`,
    not represented on the wire. `EmailMessage` rows reference this.
    """

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
    """Persisted inbound or outbound email.

    Storage form of `models.email.NormalizedInboundEmail` (direction='inbound')
    and `models.email.NormalizedOutboundEmail` (direction='outbound'). Mapper
    will live in `domain/run_recorder.py` (later slice).

    `(assistant_id, provider_message_id)` is unique so duplicate webhook
    deliveries don't double-record.
    """

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
    """Persisted form of `models.email.EmailAttachment`.

    Differs from the wire model: stores `storage_path` (file on disk) instead
    of inline `data: bytes`. Mapper writes the bytes out, then records the path.
    """

    __tablename__ = "email_attachments"

    id: Mapped[str] = _str_pk()
    message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(String(1024))


class MessageIndex(Base):
    """Lookup table for thread resolution by RFC-822 `Message-ID` header.

    Populated for both inbound and outbound messages, scoped per assistant.
    `domain/thread_resolver.py` reads it to match `In-Reply-To` / `References`
    against prior messages without scanning `email_messages` end-to-end.
    """

    __tablename__ = "message_index"

    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"), primary_key=True)
    message_id_header: Mapped[str] = mapped_column(String(998), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("email_threads.id"))
    provider_message_id: Mapped[str] = mapped_column(String(255))

    __table_args__ = (UniqueConstraint("assistant_id", "message_id_header"),)


class AgentRun(Base):
    """One agent execution, from inbound email to outbound reply (or failure).

    Operational record — written by `domain/run_recorder.py` on every accepted
    inbound, including budget-limited and failed runs. Has no wire counterpart.
    """

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
    triggered_by_scheduled_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("scheduled_tasks.id", ondelete="SET NULL"), nullable=True
    )


class RunStep(Base):
    """One step within an `AgentRun` (a tool call, model call, etc.).

    Powers the admin trace view. No wire counterpart; written by `RunRecorder`.
    """

    __tablename__ = "run_steps"

    id: Mapped[str] = _str_pk()
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    kind: Mapped[str] = mapped_column(String(32))
    input_summary: Mapped[str] = mapped_column(Text)
    output_summary: Mapped[str] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(USD_NUMERIC, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunMemoryRecall(Base):
    """One memory chunk that was injected into an agent run's prompt.

    Persisted by the runtime immediately after `MemoryPort.recall(...)`
    so the admin trace view can show what context the agent actually
    saw — re-running recall later isn't faithful (memory may have grown).
    No wire counterpart.
    """

    __tablename__ = "run_memory_recalls"

    id: Mapped[str] = _str_pk()
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    memory_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageLedger(Base):
    """Token + cost record per run, per provider/model.

    Source of truth for `domain/budget_governor.py`. No wire counterpart.
    """

    __tablename__ = "usage_ledger"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(USD_NUMERIC)
    budget_period: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduledTaskRow(Base):
    """Persisted form of `models.scheduled.ScheduledTask`.

    Driver `tick_scheduled_tasks` (procrastinate periodic, once per minute)
    claims rows where `status='active'` and `next_run_at <= now()` using
    `SELECT ... FOR UPDATE SKIP LOCKED` so concurrent ticks can't double-fire.
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    kind: Mapped[str] = mapped_column(String(16))
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cron_expr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    name: Mapped[str] = mapped_column(String(998))
    body: Mapped[str] = mapped_column(Text)
    created_by_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Budget(Base):
    """Monthly spend cap for one assistant. DB-only; surfaced in `AssistantScope`."""

    __tablename__ = "budgets"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    monthly_limit_usd: Mapped[Decimal] = mapped_column(USD_NUMERIC)
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
    "ScheduledTaskRow",
    "UsageLedger",
]
