from datetime import UTC, datetime
from pathlib import Path

from email_agent.mail.eml import parse_eml_file


def test_parses_minimal_eml(tmp_path: Path):
    eml = tmp_path / "msg.eml"
    eml.write_bytes(
        b"From: Mum <mum@example.com>\r\n"
        b"To: assistant@assistants.example.com\r\n"
        b"Subject: Question?\r\n"
        b"Message-ID: <m1@x>\r\n"
        b"Date: Sun, 10 May 2026 12:00:00 +0000\r\n"
        b"\r\n"
        b"hello\n"
    )

    parsed = parse_eml_file(eml)

    assert parsed.from_email == "mum@example.com"
    assert parsed.to_emails == ["assistant@assistants.example.com"]
    assert parsed.subject == "Question?"
    assert parsed.message_id_header == "<m1@x>"
    assert parsed.body_text.strip() == "hello"
    assert parsed.received_at == datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


def test_preserves_threading_headers(tmp_path: Path):
    eml = tmp_path / "msg.eml"
    eml.write_bytes(
        b"From: a@x\r\n"
        b"To: b@y\r\n"
        b"Subject: Re: thread\r\n"
        b"Message-ID: <m2@x>\r\n"
        b"In-Reply-To: <m1@x>\r\n"
        b"References: <m0@x> <m1@x>\r\n"
        b"\r\n"
        b"reply\n"
    )

    parsed = parse_eml_file(eml)

    assert parsed.in_reply_to_header == "<m1@x>"
    assert parsed.references_headers == ["<m0@x>", "<m1@x>"]


def test_extracts_attachments(tmp_path: Path):
    eml = tmp_path / "msg.eml"
    eml.write_bytes(
        b"From: a@x\r\n"
        b"To: b@y\r\n"
        b"Subject: with attachment\r\n"
        b"Message-ID: <m3@x>\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"body\r\n"
        b"--BOUND\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="report.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"JVBERi0xLjcK\r\n"  # %PDF-1.7\n
        b"--BOUND--\r\n"
    )

    parsed = parse_eml_file(eml)

    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.filename == "report.pdf"
    assert att.content_type == "application/pdf"
    assert att.data == b"%PDF-1.7\n"
