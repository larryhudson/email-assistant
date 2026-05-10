from datetime import UTC, datetime
from pathlib import Path

from email_agent.db.models import EmailAttachmentRow, EmailMessage, EmailThread
from email_agent.domain.workspace_projector import EmailWorkspaceProjector


def _thread() -> EmailThread:
    return EmailThread(
        id="t-1",
        assistant_id="a-1",
        end_user_id="u-1",
        root_message_id="<m1@x>",
        subject_normalized="Question",
        created_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


def _msg(**kw) -> EmailMessage:
    return EmailMessage(
        id=kw.pop("id", "m-1"),
        thread_id=kw.pop("thread_id", "t-1"),
        assistant_id=kw.pop("assistant_id", "a-1"),
        direction=kw.pop("direction", "inbound"),
        provider_message_id=kw.pop("provider_message_id", "prov-1"),
        message_id_header=kw.pop("message_id_header", "<m1@x>"),
        in_reply_to_header=kw.pop("in_reply_to_header", None),
        references_headers=kw.pop("references_headers", []),
        from_email=kw.pop("from_email", "mum@example.com"),
        to_emails=kw.pop("to_emails", ["mum@assistants.example.com"]),
        subject=kw.pop("subject", "Question?"),
        body_text=kw.pop("body_text", "hello"),
        body_html=kw.pop("body_html", None),
        created_at=kw.pop("created_at", datetime(2026, 5, 10, 12, 0, tzinfo=UTC)),
    )


def test_projects_thread_messages_and_attachments(tmp_path: Path) -> None:
    # Source attachment file already on disk (the persister writes it earlier).
    attachments_root = tmp_path / "attachments"
    attachments_root.mkdir()
    src_pdf = attachments_root / "report.pdf"
    src_pdf.write_bytes(b"%PDF-1.7 hello")

    thread = _thread()
    messages = [
        _msg(
            id="m-1",
            message_id_header="<m1@x>",
            from_email="mum@example.com",
            body_text="first inbound",
            created_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
        ),
        _msg(
            id="m-2",
            message_id_header="<m2@x>",
            direction="outbound",
            from_email="mum@assistants.example.com",
            to_emails=["mum@example.com"],
            body_text="reply",
            created_at=datetime(2026, 5, 10, 12, 5, tzinfo=UTC),
        ),
        _msg(
            id="m-3",
            message_id_header="<m3@x>",
            from_email="mum@example.com",
            body_text="another inbound",
            created_at=datetime(2026, 5, 10, 12, 10, tzinfo=UTC),
        ),
    ]
    attachments = [
        EmailAttachmentRow(
            id="a-1",
            message_id="m-3",
            filename="report.pdf",
            content_type="application/pdf",
            size_bytes=src_pdf.stat().st_size,
            storage_path=str(src_pdf),
        )
    ]

    projector = EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs")
    result = projector.project(
        run_id="r-1",
        thread=thread,
        messages=messages,
        attachments=attachments,
        current_message_id="m-3",
    )

    emails_dir = tmp_path / "run_inputs" / "r-1" / "emails" / "t-1"
    assert emails_dir.is_dir()
    assert result.emails_dir == emails_dir

    # thread.md exists with subject + participants
    thread_md = (emails_dir / "thread.md").read_text()
    assert "Question" in thread_md
    assert "mum@example.com" in thread_md
    assert "mum@assistants.example.com" in thread_md

    # one .md per message, ordered
    files = sorted(p.name for p in emails_dir.glob("*.md") if p.name != "thread.md")
    assert files == [
        "0001-2026-05-10-from-mum-at-example-com.md",
        "0002-2026-05-10-from-mum-at-assistants-example-com.md",
        "0003-2026-05-10-from-mum-at-example-com.md",
    ]

    # frontmatter contains expected keys
    first_msg = (emails_dir / files[0]).read_text()
    for key in ("from:", "to:", "date:", "subject:", "message_id:"):
        assert key in first_msg
    assert "first inbound" in first_msg

    # attachments copied with NNNN- prefix (1 because it's the only attachment)
    att_dir = emails_dir / "attachments"
    assert (att_dir / "0001-report.pdf").read_bytes() == b"%PDF-1.7 hello"

    # current_message_path points at the third message file (sandbox-relative)
    assert result.current_message_path == "emails/t-1/0003-2026-05-10-from-mum-at-example-com.md"


def test_wipes_stale_run_dir_before_regenerating(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_inputs" / "r-1"
    stale_dir = run_dir / "emails" / "dead-thread"
    stale_dir.mkdir(parents=True)
    (stale_dir / "ghost.md").write_text("should be deleted")

    projector = EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs")
    projector.project(
        run_id="r-1",
        thread=_thread(),
        messages=[_msg()],
        attachments=[],
        current_message_id="m-1",
    )

    assert not stale_dir.exists()
    assert not (run_dir / "emails" / "dead-thread").exists()
