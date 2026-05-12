import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from email_agent.db.models import EmailAttachmentRow, EmailMessage, EmailThread


@dataclass(frozen=True)
class ProjectionResult:
    """Pointers the runtime needs after laying out threads for the sandbox.

    `run_inputs_dir` is the host-side root that gets bind-mounted read-only
    into the container at /workspace/emails/. `current_message_path` is the
    sandbox-relative path the prompt should reference. `emails_dir` is the
    per-thread directory for the *current* thread — handy for tests, not
    used by the runtime.
    """

    run_inputs_dir: Path
    emails_dir: Path
    current_message_path: str


class EmailWorkspaceProjector:
    """Lays out every thread for an assistant as a deterministic file tree.

    Output rooted at `<run_inputs_root>/<run_id>/emails/`:
      - `INDEX.md`                        — manifest of every thread (subject,
                                            last activity, message count, dir)
      - `YYYY-MM-DD-<thread_id>/thread.md` — subject + participants
      - `YYYY-MM-DD-<thread_id>/NNNN-YYYY-MM-DD-from-<who>.md` — one per
                                            message in that thread
      - `YYYY-MM-DD-<thread_id>/attachments/NNNN-<filename>` — copies of
                                            stored attachments for that thread

    The directory name is prefixed with the thread's last-activity date so a
    plain `ls /workspace/emails/` sorts chronologically (oldest → newest).
    INDEX.md surfaces the reverse — most-recent first — to make recency the
    default scanning order when the agent reads the manifest.

    The run-inputs directory is wiped and rebuilt on every call so the agent
    always sees the current DB truth. Bind-mounted read-only into the
    container at /workspace/emails/, so writes from inside the container can
    never poison this view.

    Threads other than the currently-handled one are included so the agent
    can read history across conversations with the same end-user.
    """

    def __init__(self, *, run_inputs_root: Path) -> None:
        self._run_inputs_root = run_inputs_root

    def project(
        self,
        *,
        run_id: str,
        threads: Iterable[EmailThread],
        messages: Iterable[EmailMessage],
        attachments: Iterable[EmailAttachmentRow],
        current_thread_id: str,
        current_message_id: str,
    ) -> ProjectionResult:
        run_dir = self._run_inputs_root / run_id
        emails_root = run_dir / "emails"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        emails_root.mkdir(parents=True)

        messages_by_thread: dict[str, list[EmailMessage]] = {}
        for m in messages:
            messages_by_thread.setdefault(m.thread_id, []).append(m)
        attachments_by_message = _group_attachments(attachments)

        threads_list = list(threads)
        if not any(t.id == current_thread_id for t in threads_list):
            raise ValueError(f"current_thread_id={current_thread_id!r} not in supplied threads")

        current_filename: str | None = None
        current_thread_dirname: str | None = None
        index_rows: list[tuple[EmailThread, int, str, str]] = []

        for thread in threads_list:
            thread_msgs = sorted(
                messages_by_thread.get(thread.id, []),
                key=lambda m: (m.created_at, m.id),
            )
            last_activity_dt = (
                max(m.created_at for m in thread_msgs) if thread_msgs else thread.updated_at
            )
            dirname = f"{last_activity_dt.strftime('%Y-%m-%d')}-{thread.id}"
            thread_dir = emails_root / dirname
            thread_dir.mkdir()
            att_dir = thread_dir / "attachments"
            att_dir.mkdir()

            participants = sorted(
                {m.from_email for m in thread_msgs}
                | {addr for m in thread_msgs for addr in m.to_emails}
            )
            thread_md = (
                "---\n"
                f"thread_id: {thread.id}\n"
                f"subject: {thread.subject_normalized}\n"
                "participants:\n" + "".join(f"  - {addr}\n" for addr in participants) + "---\n"
            )
            (thread_dir / "thread.md").write_text(thread_md)

            message_filenames: dict[str, str] = {}
            for index, message in enumerate(thread_msgs, start=1):
                filename = _message_filename(index, message)
                message_filenames[message.id] = filename
                (thread_dir / filename).write_text(_message_markdown(message))

            attachment_index = 0
            for message in thread_msgs:
                for att in attachments_by_message.get(message.id, []):
                    attachment_index += 1
                    dest = att_dir / f"{attachment_index:04d}-{att.filename}"
                    shutil.copyfile(att.storage_path, dest)

            if thread.id == current_thread_id:
                current_filename = message_filenames.get(current_message_id)
                current_thread_dirname = dirname

            index_rows.append((thread, len(thread_msgs), last_activity_dt.isoformat(), dirname))

        if current_filename is None or current_thread_dirname is None:
            raise ValueError(
                f"current_message_id={current_message_id!r} not in thread {current_thread_id!r}"
            )

        (emails_root / "INDEX.md").write_text(_render_index(index_rows, current_thread_id))

        current_message_path = f"emails/{current_thread_dirname}/{current_filename}"
        return ProjectionResult(
            run_inputs_dir=run_dir,
            emails_dir=emails_root / current_thread_dirname,
            current_message_path=current_message_path,
        )


def _render_index(
    rows: list[tuple[EmailThread, int, str, str]],
    current_thread_id: str,
) -> str:
    # Sort by last activity desc so the most recent thread is on top.
    rows_sorted = sorted(rows, key=lambda r: r[2], reverse=True)
    lines = [
        "# Threads with this end-user",
        "",
        "Every prior conversation with the same end-user is projected below — "
        "use `read` to inspect any of them when looking for context. Directory "
        "names are prefixed with the thread's last-activity date so `ls` sorts "
        "them chronologically. The thread marked **(current)** is the one that "
        "just received a new inbound.",
        "",
    ]
    for thread, count, last_activity, dirname in rows_sorted:
        marker = " **(current)**" if thread.id == current_thread_id else ""
        subject = thread.subject_normalized or "(no subject)"
        lines.append(
            f"- `{dirname}/`{marker} — {subject} "
            f"({count} message{'s' if count != 1 else ''}, last activity {last_activity})"
        )
    return "\n".join(lines) + "\n"


def _group_attachments(
    attachments: Iterable[EmailAttachmentRow],
) -> dict[str, list[EmailAttachmentRow]]:
    out: dict[str, list[EmailAttachmentRow]] = {}
    for att in attachments:
        out.setdefault(att.message_id, []).append(att)
    return out


def _message_filename(index: int, message: EmailMessage) -> str:
    date = message.created_at.strftime("%Y-%m-%d")
    sender = _sanitize_email_for_filename(message.from_email)
    return f"{index:04d}-{date}-from-{sender}.md"


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _sanitize_email_for_filename(address: str) -> str:
    cleaned = _NON_ALNUM.sub("-", address.lower().replace("@", "-at-")).strip("-")
    return cleaned or "unknown"


def _message_markdown(message: EmailMessage) -> str:
    references = " ".join(message.references_headers) if message.references_headers else ""
    in_reply_to = message.in_reply_to_header or ""
    to_emails = ", ".join(message.to_emails)
    return (
        "---\n"
        f"direction: {message.direction}\n"
        f"from: {message.from_email}\n"
        f"to: {to_emails}\n"
        f"date: {message.created_at.isoformat()}\n"
        f"subject: {_quote_yaml(message.subject)}\n"
        f"message_id: {message.message_id_header}\n"
        f"in_reply_to: {in_reply_to}\n"
        f"references: {references}\n"
        "---\n"
        f"\n{message.body_text}\n"
    )


def _quote_yaml(value: str) -> str:
    if any(ch in value for ch in (":", '"', "'", "\n")):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


__all__ = ["EmailWorkspaceProjector", "ProjectionResult"]
