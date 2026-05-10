from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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
