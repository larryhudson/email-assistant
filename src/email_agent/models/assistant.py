from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from email_agent.db.models import Assistant as AssistantRow
    from email_agent.db.models import AssistantScopeRow, EndUser, Owner


class AssistantStatus(StrEnum):
    """Operational state of an assistant. Drives whether webhooks for it run."""

    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class AssistantScope(BaseModel):
    """Everything the runtime needs to execute a single agent run for an assistant.

    Loaded by `AssistantRouter` from the `assistants` + `assistant_scopes` rows
    once per inbound email and passed through every downstream stage (budget,
    workspace projection, agent invocation, reply, recording). Frozen so a
    request's view of an assistant can't drift mid-run if the DB changes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    assistant_id: str
    owner_id: str
    end_user_id: str
    inbound_address: str
    status: AssistantStatus
    allowed_senders: tuple[str, ...]
    memory_namespace: str
    tool_allowlist: tuple[str, ...]
    budget_id: str
    model_name: str
    system_prompt: str

    def is_sender_allowed(self, email: str) -> bool:
        """True if the sender is in this assistant's allowlist (case-insensitive).

        Untrusted senders are dropped at the webhook stage; this is the gate.
        """
        target = email.lower()
        return any(s.lower() == target for s in self.allowed_senders)

    @classmethod
    def from_rows(
        cls,
        *,
        owner: "Owner",
        end_user: "EndUser",
        assistant: "AssistantRow",
        scope_row: "AssistantScopeRow",
    ) -> "AssistantScope":
        """Flatten the three persisted rows into the wire-side scope.

        Lives on the wire model because that's the side that owns the shape;
        the ORM is a dumb storage representation.
        """
        return cls(
            assistant_id=assistant.id,
            owner_id=owner.id,
            end_user_id=end_user.id,
            inbound_address=assistant.inbound_address,
            status=AssistantStatus(assistant.status),
            allowed_senders=tuple(assistant.allowed_senders),
            memory_namespace=scope_row.memory_namespace,
            tool_allowlist=tuple(scope_row.tool_allowlist),
            budget_id=scope_row.budget_id,
            model_name=assistant.model,
            system_prompt=assistant.system_prompt,
        )
