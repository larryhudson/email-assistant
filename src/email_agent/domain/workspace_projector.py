import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from email_agent.db.models import EmailAttachmentRow, EmailMessage, EmailThread


@dataclass(frozen=True)
class ProjectionResult:
    """Pointers the runtime needs after laying out a thread for the sandbox.

    `run_inputs_dir` is the host-side root that gets bind-mounted read-only
    into the container at /workspace/emails/. `current_message_path` is the
    sandbox-relative path the prompt should reference.
    """

    run_inputs_dir: Path
    emails_dir: Path
    current_message_path: str


class EmailWorkspaceProjector:
    """Lays out an email thread as a deterministic file tree per run.

    Output rooted at `<run_inputs_root>/<run_id>/emails/<thread_id>/`:
      - `thread.md`             — subject + participants
      - `NNNN-YYYY-MM-DD-from-<who>.md` — one per message, ordered
      - `attachments/NNNN-<filename>`   — copies of stored attachments

    The directory is wiped and rebuilt on every call so the agent always sees
    the current DB truth. Bind-mounted read-only into the container at
    /workspace/emails/, so writes from inside the container can never poison
    this view.
    """

    def __init__(self, *, run_inputs_root: Path) -> None:
        self._run_inputs_root = run_inputs_root

    def project(
        self,
        *,
        run_id: str,
        thread: EmailThread,
        messages: Iterable[EmailMessage],
        attachments: Iterable[EmailAttachmentRow],
        current_message_id: str,
    ) -> ProjectionResult:
        run_dir = self._run_inputs_root / run_id
        emails_dir = run_dir / "emails" / thread.id
        if run_dir.exists():
            shutil.rmtree(run_dir)
        emails_dir.mkdir(parents=True)
        att_dir = emails_dir / "attachments"
        att_dir.mkdir()

        ordered = sorted(messages, key=lambda m: (m.created_at, m.id))
        attachments_by_message = _group_attachments(attachments)

        # thread.md
        participants = sorted(
            {m.from_email for m in ordered} | {addr for m in ordered for addr in m.to_emails}
        )
        thread_md = (
            "---\n"
            f"thread_id: {thread.id}\n"
            f"subject: {thread.subject_normalized}\n"
            "participants:\n" + "".join(f"  - {addr}\n" for addr in participants) + "---\n"
        )
        (emails_dir / "thread.md").write_text(thread_md)

        # one .md per message
        message_filenames: dict[str, str] = {}
        for index, message in enumerate(ordered, start=1):
            filename = _message_filename(index, message)
            message_filenames[message.id] = filename
            content = _message_markdown(message)
            (emails_dir / filename).write_text(content)

        # attachments
        attachment_index = 0
        for message in ordered:
            for att in attachments_by_message.get(message.id, []):
                attachment_index += 1
                dest = att_dir / f"{attachment_index:04d}-{att.filename}"
                shutil.copyfile(att.storage_path, dest)

        current_filename = message_filenames[current_message_id]
        current_message_path = f"emails/{thread.id}/{current_filename}"

        return ProjectionResult(
            run_inputs_dir=run_dir,
            emails_dir=emails_dir,
            current_message_path=current_message_path,
        )


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
