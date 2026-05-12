from datetime import UTC, datetime
from pathlib import Path

import pytest

from email_agent.db.models import EmailAttachmentRow, EmailMessage, EmailThread
from email_agent.domain.workspace_projector import EmailWorkspaceProjector


def _thread(
    *,
    id: str = "t-1",
    subject: str = "Question",
    updated_at: datetime = datetime(2026, 5, 10, 12, 10, tzinfo=UTC),
) -> EmailThread:
    return EmailThread(
        id=id,
        assistant_id="a-1",
        end_user_id="u-1",
        root_message_id=f"<{id}@x>",
        subject_normalized=subject,
        created_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
        updated_at=updated_at,
    )


def _msg(
    *,
    id: str = "m-1",
    thread_id: str = "t-1",
    direction: str = "inbound",
    message_id_header: str = "<m1@x>",
    from_email: str = "mum@example.com",
    to_emails: list[str] | None = None,
    subject: str = "Question?",
    body_text: str = "hello",
    created_at: datetime = datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
) -> EmailMessage:
    return EmailMessage(
        id=id,
        thread_id=thread_id,
        assistant_id="a-1",
        direction=direction,
        provider_message_id=f"prov-{id}",
        message_id_header=message_id_header,
        in_reply_to_header=None,
        references_headers=[],
        from_email=from_email,
        to_emails=to_emails or ["mum@assistants.example.com"],
        subject=subject,
        body_text=body_text,
        body_html=None,
        created_at=created_at,
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
        threads=[thread],
        messages=messages,
        attachments=attachments,
        current_thread_id="t-1",
        current_message_id="m-3",
    )

    # Thread directories are prefixed with the last-activity date so `ls`
    # sorts chronologically. Current thread's latest message is 2026-05-10.
    emails_dir = tmp_path / "run_inputs" / "r-1" / "emails" / "2026-05-10-t-1"
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
    assert (
        result.current_message_path
        == "emails/2026-05-10-t-1/0003-2026-05-10-from-mum-at-example-com.md"
    )

    # INDEX.md is generated and references the single thread as current.
    index = (tmp_path / "run_inputs" / "r-1" / "emails" / "INDEX.md").read_text()
    assert "2026-05-10-t-1/" in index
    assert "**(current)**" in index
    assert "Question" in index


def test_projects_all_threads_with_index_for_assistant(tmp_path: Path) -> None:
    """Multiple threads land under emails/<thread_id>/ and the INDEX names them."""
    other = _thread(
        id="t-2",
        subject="Older chat",
        updated_at=datetime(2026, 5, 9, 9, 0, tzinfo=UTC),
    )
    current = _thread()  # t-1, newer

    messages = [
        _msg(
            id="m-old",
            thread_id="t-2",
            body_text="old hello",
            created_at=datetime(2026, 5, 9, 9, 0, tzinfo=UTC),
        ),
        _msg(id="m-1", thread_id="t-1", body_text="current hello"),
    ]

    projector = EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs")
    result = projector.project(
        run_id="r-1",
        threads=[current, other],
        messages=messages,
        attachments=[],
        current_thread_id="t-1",
        current_message_id="m-1",
    )

    emails_root = tmp_path / "run_inputs" / "r-1" / "emails"
    assert (emails_root / "2026-05-10-t-1" / "thread.md").is_file()
    assert (emails_root / "2026-05-09-t-2" / "thread.md").is_file()
    # Each thread has its own message file(s).
    assert any((emails_root / "2026-05-09-t-2").glob("0001-*.md"))
    # Index lists both, with the current marked.
    index = (emails_root / "INDEX.md").read_text()
    assert "2026-05-10-t-1/" in index
    assert "2026-05-09-t-2/" in index
    assert "Older chat" in index
    # Most-recently-active first: t-1 line should precede t-2 line.
    assert index.index("2026-05-10-t-1/") < index.index("2026-05-09-t-2/")
    # current_message_path still points at the current thread's inbound.
    assert (
        result.current_message_path
        == "emails/2026-05-10-t-1/0001-2026-05-10-from-mum-at-example-com.md"
    )


def test_rejects_current_thread_not_in_threads(tmp_path: Path) -> None:
    projector = EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs")
    with pytest.raises(ValueError, match="current_thread_id"):
        projector.project(
            run_id="r-1",
            threads=[_thread(id="t-2")],
            messages=[_msg(thread_id="t-2", id="m-1")],
            attachments=[],
            current_thread_id="t-1",
            current_message_id="m-1",
        )


def test_wipes_stale_run_dir_before_regenerating(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_inputs" / "r-1"
    stale_dir = run_dir / "emails" / "dead-thread"
    stale_dir.mkdir(parents=True)
    (stale_dir / "ghost.md").write_text("should be deleted")

    projector = EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs")
    projector.project(
        run_id="r-1",
        threads=[_thread()],
        messages=[_msg()],
        attachments=[],
        current_thread_id="t-1",
        current_message_id="m-1",
    )

    assert not stale_dir.exists()
    assert not (run_dir / "emails" / "dead-thread").exists()


def test_sanitizes_complicated_sender_and_quotes_subject(tmp_path: Path) -> None:
    projector = EmailWorkspaceProjector(run_inputs_root=tmp_path / "run_inputs")
    result = projector.project(
        run_id="r-1",
        threads=[_thread()],
        messages=[
            _msg(
                id="m-1",
                from_email="Mum O'Connor <Mum.O'Connor@example.CO.UK>",
                subject='Re: a/b "thing": status',
            )
        ],
        attachments=[],
        current_thread_id="t-1",
        current_message_id="m-1",
    )

    assert result.current_message_path.endswith(
        "0001-2026-05-10-from-mum-o-connor-mum-o-connor-at-example-co-uk.md"
    )
    body = (
        result.emails_dir / "0001-2026-05-10-from-mum-o-connor-mum-o-connor-at-example-co-uk.md"
    ).read_text()
    assert 'subject: "Re: a/b \\"thing\\": status"' in body
