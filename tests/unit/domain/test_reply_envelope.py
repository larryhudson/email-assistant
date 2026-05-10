from datetime import UTC, datetime

from email_agent.domain.reply_envelope import ReplyEnvelopeBuilder
from email_agent.models.email import EmailAttachment, NormalizedInboundEmail


def _inbound(
    *, subject: str = "Question?", references: list[str] | None = None
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id="prov-1",
        message_id_header="<m1@x>",
        in_reply_to_header=None,
        references_headers=references if references is not None else ["<r0@x>"],
        from_email="mum@example.com",
        to_emails=["mum@assistants.example.com"],
        subject=subject,
        body_text="hello",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


def test_builds_envelope_with_threading_headers_and_re_prefix() -> None:
    builder = ReplyEnvelopeBuilder()
    envelope = builder.build(
        inbound=_inbound(),
        from_email="mum@assistants.example.com",
        body_text="ok, will do",
        attachments=[],
        message_id_factory=lambda: "<run-abc@assistants.example.com>",
    )

    assert envelope.from_email == "mum@assistants.example.com"
    assert envelope.to_emails == ["mum@example.com"]
    assert envelope.subject == "Re: Question?"
    assert envelope.body_text == "ok, will do"
    assert envelope.message_id_header == "<run-abc@assistants.example.com>"
    assert envelope.in_reply_to_header == "<m1@x>"
    assert envelope.references_headers == ["<r0@x>", "<m1@x>"]


def test_does_not_double_prefix_re() -> None:
    builder = ReplyEnvelopeBuilder()
    envelope = builder.build(
        inbound=_inbound(subject="re: prior"),
        from_email="mum@assistants.example.com",
        body_text="ack",
        attachments=[],
        message_id_factory=lambda: "<run-abc@x>",
    )

    assert envelope.subject == "re: prior"


def test_passes_attachments_through() -> None:
    builder = ReplyEnvelopeBuilder()
    pdf = EmailAttachment(
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=8,
        data=b"%PDF-1.7",
    )
    envelope = builder.build(
        inbound=_inbound(),
        from_email="mum@assistants.example.com",
        body_text="see attached",
        attachments=[pdf],
        message_id_factory=lambda: "<run-abc@x>",
    )

    assert envelope.attachments == [pdf]


def test_starts_references_chain_when_inbound_has_none() -> None:
    builder = ReplyEnvelopeBuilder()
    envelope = builder.build(
        inbound=_inbound(references=[]),
        from_email="mum@assistants.example.com",
        body_text="x",
        attachments=[],
        message_id_factory=lambda: "<run-abc@x>",
    )

    assert envelope.references_headers == ["<m1@x>"]
