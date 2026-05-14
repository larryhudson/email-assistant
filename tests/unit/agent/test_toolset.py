import subprocess
from decimal import Decimal

from pydantic_ai import BinaryContent, ToolReturn

from email_agent.agent.toolset import AgentToolset
from email_agent.github.port import GitHubRepository
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.agent import MeteredUsage
from email_agent.models.sandbox import PendingAttachment
from email_agent.pdf.port import PdfGenerationResult, PdfPreviewResult
from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace
from email_agent.search.inmemory import InMemorySearchAdapter
from email_agent.search.port import SearchResult


def _toolset(
    env: InMemoryEnvironment,
    *,
    memory: InMemoryMemoryAdapter | None = None,
    pending: list[PendingAttachment] | None = None,
    metered: list[MeteredUsage] | None = None,
    search: InMemorySearchAdapter | None = None,
    pdf_renderer: object | None = None,
    github: object | None = None,
    github_clone_runner=None,
) -> AgentToolset:
    return AgentToolset(
        assistant_id="a-1",
        run_id="r-1",
        env=env,
        workspace=AssistantWorkspace(env),
        memory=memory or InMemoryMemoryAdapter(),
        pending_attachments=pending if pending is not None else [],
        metered_usage=metered if metered is not None else [],
        search=search,
        pdf_renderer=pdf_renderer,  # ty: ignore[invalid-argument-type]
        github=github,  # ty: ignore[invalid-argument-type]
        github_clone_runner=github_clone_runner,
    )


class _FakePdfRenderer:
    def __init__(self) -> None:
        self.generate_calls: list[tuple[str, str]] = []
        self.preview_calls: list[tuple[str, int, int]] = []

    async def generate_pdf(
        self,
        env,
        *,
        html_path: str,
        output_path: str,
    ) -> PdfGenerationResult:
        self.generate_calls.append((html_path, output_path))
        await env.write_bytes(output_path, b"%PDF fake")
        return PdfGenerationResult(pdf_path=output_path, size_bytes=9)

    async def preview_pdf(
        self,
        env,
        *,
        pdf_path: str,
        page: int = 1,
        dpi: int = 160,
    ) -> PdfPreviewResult:
        self.preview_calls.append((pdf_path, page, dpi))
        return PdfPreviewResult(
            pdf_path=pdf_path,
            page=page,
            page_count=2,
            dpi=dpi,
            png_bytes=b"\x89PNG\r\n\x1a\n",
        )


class _FakeGitHub:
    username = "larryhudson"

    def __init__(self) -> None:
        self.repos = [
            GitHubRepository(
                name="email-assistant",
                full_name="larryhudson/email-assistant",
                clone_url="https://github.com/larryhudson/email-assistant.git",
                private=False,
                description="Email agent",
            )
        ]

    async def list_owned_repositories(self):
        return self.repos

    async def get_owned_repository(self, name: str):
        return next((repo for repo in self.repos if repo.name == name), None)


async def test_read_returns_file_contents() -> None:
    env = InMemoryEnvironment()
    await env.write_text("notes/draft.md", "hello")

    assert await _toolset(env).read("notes/draft.md") == "hello"


async def test_read_returns_error_text_instead_of_raising() -> None:
    result = await _toolset(InMemoryEnvironment()).read("missing.md")

    assert "ERROR: read(missing.md) failed" in result
    assert "missing.md" in result


async def test_write_rejects_emails_directory_and_writes_other_paths() -> None:
    env = InMemoryEnvironment()
    toolset = _toolset(env)

    rejected = await toolset.write("emails/x.md", "x")
    written = await toolset.write("notes/draft.md", "hello")

    assert "ERROR: write(emails/x.md) failed" in rejected
    assert "read-only" in rejected
    assert written == "wrote notes/draft.md"
    assert await env.read_text("notes/draft.md") == "hello"


async def test_edit_replaces_first_match_and_reports_missing_old_text() -> None:
    env = InMemoryEnvironment()
    await env.write_text("notes/draft.md", "hello hello")
    toolset = _toolset(env)

    edited = await toolset.edit("notes/draft.md", "hello", "hi")
    missing = await toolset.edit("notes/draft.md", "nope", "x")

    assert edited == "edited notes/draft.md"
    assert await env.read_text("notes/draft.md") == "hi hello"
    assert "ERROR: edit(notes/draft.md) failed" in missing
    assert "old string not found" in missing


async def test_bash_returns_existing_model_facing_format() -> None:
    result = await _toolset(InMemoryEnvironment()).bash("printf hello")

    assert result == "exit_code=0\nstdout:\nhello\nstderr:\n"


async def test_attach_file_appends_pending_attachment() -> None:
    env = InMemoryEnvironment()
    await env.write_text("out/report.txt", "report")
    pending: list[PendingAttachment] = []
    toolset = _toolset(env, pending=pending)

    result = await toolset.attach_file("out/report.txt")

    assert result == "attached out/report.txt"
    assert pending == [PendingAttachment(sandbox_path="out/report.txt", filename="report.txt")]


async def test_attach_file_returns_error_for_missing_file() -> None:
    result = await _toolset(InMemoryEnvironment()).attach_file("missing.txt")

    assert "ERROR: attach_file(missing.txt) failed" in result
    assert "not found" in result


async def test_generate_pdf_renders_html_to_default_pdf_path() -> None:
    env = InMemoryEnvironment()
    await env.write_text("docs/report.html", "<h1>Report</h1>")
    renderer = _FakePdfRenderer()

    result = await _toolset(env, pdf_renderer=renderer).generate_pdf("docs/report.html")

    assert result == "generated docs/report.pdf (9 bytes)"
    assert renderer.generate_calls == [("docs/report.html", "docs/report.pdf")]
    assert await env.read_bytes("docs/report.pdf") == b"%PDF fake"


async def test_generate_pdf_rejects_readonly_email_output() -> None:
    env = InMemoryEnvironment()
    await env.write_text("docs/report.html", "<h1>Report</h1>")

    result = await _toolset(env, pdf_renderer=_FakePdfRenderer()).generate_pdf(
        "docs/report.html",
        "emails/report.pdf",
    )

    assert "ERROR: generate_pdf(emails/report.pdf) failed" in result
    assert "read-only" in result


async def test_preview_pdf_returns_png_tool_content() -> None:
    env = InMemoryEnvironment()
    await env.write_bytes("docs/report.pdf", b"%PDF fake")
    renderer = _FakePdfRenderer()

    result = await _toolset(env, pdf_renderer=renderer).preview_pdf(
        "docs/report.pdf",
        page=2,
        dpi=180,
    )

    assert isinstance(result, ToolReturn)
    assert result.return_value == "previewed docs/report.pdf page 2/2 at 180 dpi"
    assert renderer.preview_calls == [("docs/report.pdf", 2, 180)]
    assert result.content is not None
    assert result.content[0] == "previewed docs/report.pdf page 2/2 at 180 dpi"
    image_content = result.content[1]
    assert isinstance(image_content, BinaryContent)
    assert image_content.media_type == "image/png"
    assert image_content.data == b"\x89PNG\r\n\x1a\n"


async def test_memory_search_delegates_by_assistant_id() -> None:
    memory = InMemoryMemoryAdapter()
    await memory.record_turn("a-1", "t-1", "assistant", "likes short replies")
    await memory.record_turn("a-2", "t-1", "assistant", "other assistant")

    result = await _toolset(InMemoryEnvironment(), memory=memory).memory_search("short")

    assert isinstance(result, list)
    assert [m.content for m in result] == ["[t-1/assistant] likes short replies"]


async def test_web_search_runs_on_host_adapter_and_records_metered_usage() -> None:
    metered: list[MeteredUsage] = []
    search = InMemorySearchAdapter(
        results=[
            SearchResult(
                title="Result title",
                url="https://example.com/news",
                snippet="Fresh public web content",
                age="1 day ago",
            )
        ],
        cost_usd=Decimal("0.0050"),
    )

    result = await _toolset(
        InMemoryEnvironment(),
        metered=metered,
        search=search,
    ).web_search("latest thing", max_results=3)

    assert search.calls == [("latest thing", 3)]
    assert "UNTRUSTED EXTERNAL WEB SEARCH RESULTS" in result
    assert "not from the user" in result
    assert "Fresh public web content" in result
    assert metered == [
        MeteredUsage(
            provider="brave",
            model="web-search",
            cost_usd=Decimal("0.0050"),
            tool_name="web_search",
        )
    ]


async def test_list_github_repositories_only_reports_owned_repositories() -> None:
    result = await _toolset(InMemoryEnvironment(), github=_FakeGitHub()).list_github_repositories()

    assert "Repositories owned by larryhudson" in result
    assert "email-assistant (public) - Email agent" in result


async def test_clone_github_repository_rejects_other_owner() -> None:
    result = await _toolset(InMemoryEnvironment(), github=_FakeGitHub()).clone_github_repository(
        "someone-else/email-assistant"
    )

    assert "ERROR: clone_github_repository(someone-else/email-assistant) failed" in result
    assert "owned by larryhudson" in result


async def test_clone_github_repository_clones_on_host_and_projects_files() -> None:
    calls: list[tuple[str, str]] = []

    def clone_runner(clone_url, destination):
        calls.append((clone_url, str(destination)))
        (destination / "src").mkdir(parents=True)
        (destination / "src" / "app.py").write_text("print('hello')\n")
        (destination / ".git").mkdir()
        (destination / ".git" / "config").write_text("[remote]\n")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    env = InMemoryEnvironment()

    result = await _toolset(
        env, github=_FakeGitHub(), github_clone_runner=clone_runner
    ).clone_github_repository("larryhudson/email-assistant")

    assert result == "cloned larryhudson/email-assistant into repos/email-assistant"
    assert calls[0][0] == "https://github.com/larryhudson/email-assistant.git"
    assert await env.read_text("repos/email-assistant/src/app.py") == "print('hello')\n"
    assert not await env.exists("repos/email-assistant/.git/config")
