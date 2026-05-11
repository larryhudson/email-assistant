from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ScheduledTaskKind(StrEnum):
    ONCE = "once"
    CRON = "cron"


class ScheduledTaskStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class ScheduledTask(BaseModel):
    """A scheduled synthetic-inbound task owned by an assistant.

    Each fire builds a `NormalizedInboundEmail` and feeds it to the runtime,
    spawning a new agent thread. `kind='once'` populates `run_at`; `kind='cron'`
    populates `cron_expr`. `next_run_at` is the indexed column the tick uses to
    find due rows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    assistant_id: str
    kind: ScheduledTaskKind
    run_at: datetime | None
    cron_expr: str | None
    next_run_at: datetime
    last_run_at: datetime | None
    status: ScheduledTaskStatus
    subject: str
    body: str
    created_by_run_id: str | None
    created_at: datetime
    updated_at: datetime
