from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AssistantStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class AssistantScope(BaseModel):
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
        target = email.lower()
        return any(s.lower() == target for s in self.allowed_senders)
