"""Serialize/deserialize Pydantic AI `ModelMessage` history for durable storage.

Used to persist a successful run's `result.all_messages()` on `agent_runs.message_history`
so a same-thread follow-up run can be handed prior tool calls and returns via
`Agent.run(..., message_history=...)`. The serialized form is the JSON-mode dump
of `ModelMessagesTypeAdapter`, which round-trips back to live `ModelMessage`
instances via the same adapter.
"""

from typing import Any

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter


def serialize_message_history(messages: list[ModelMessage]) -> list[Any]:
    """Dump a list of `ModelMessage`s to JSON-compatible primitives."""
    return ModelMessagesTypeAdapter.dump_python(messages, mode="json")


def deserialize_message_history(payload: list[Any]) -> list[ModelMessage]:
    """Rehydrate a JSON-compatible payload back to `ModelMessage`s."""
    return ModelMessagesTypeAdapter.validate_python(payload)


__all__ = ["deserialize_message_history", "serialize_message_history"]
