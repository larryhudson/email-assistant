from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from email_agent.agent.history import (
    deserialize_message_history,
    serialize_message_history,
)


def test_serialize_round_trips_tool_call_and_return_through_json():
    """A tool call + its return must survive a JSON round-trip and rehydrate to
    the same Pydantic AI message types. This is the contract a same-thread
    follow-up run relies on when it's handed prior history via `message_history=`.
    """
    import json

    original = [
        ModelRequest(parts=[UserPromptPart(content="please action ACTION-TOKEN-XYZ")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="read",
                    args={"path": "emails/t-1/0001-msg.md"},
                    tool_call_id="call-1",
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="read",
                    content="please action ACTION-TOKEN-XYZ",
                    tool_call_id="call-1",
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content="OK, on it.")]),
    ]

    payload = serialize_message_history(original)
    # Payload is JSON-serializable as-is (the storage column is JSON).
    json.dumps(payload)

    back = deserialize_message_history(payload)
    assert [type(m).__name__ for m in back] == [
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
        "ModelResponse",
    ]

    tool_call = back[1].parts[0]
    assert isinstance(tool_call, ToolCallPart)
    assert tool_call.tool_name == "read"
    assert tool_call.args == {"path": "emails/t-1/0001-msg.md"}
    assert tool_call.tool_call_id == "call-1"

    tool_return = back[2].parts[0]
    assert isinstance(tool_return, ToolReturnPart)
    assert tool_return.tool_name == "read"
    assert tool_return.tool_call_id == "call-1"
    assert "ACTION-TOKEN-XYZ" in str(tool_return.content)

    final_text = back[3].parts[0]
    assert isinstance(final_text, TextPart)
    assert final_text.content == "OK, on it."


def test_serialize_empty_history_is_empty_list():
    assert serialize_message_history([]) == []
    assert deserialize_message_history([]) == []
