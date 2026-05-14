import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from email_agent.pdf.port import PdfGenerationResult, PdfPreviewResult
from email_agent.sandbox.environment import SandboxEnvironment

WORKSPACE_ROOT = "/workspace"


class PrincePdfRenderer:
    """Host-side HTML-to-PDF renderer for sandbox-authored files."""

    def __init__(
        self,
        *,
        prince_path: str = "prince",
        timeout_seconds: float = 30.0,
        preview_max_dpi: int = 220,
        preview_max_bytes: int = 8_000_000,
        staged_file_limit: int = 200,
        staged_bytes_limit: int = 25_000_000,
    ) -> None:
        self._prince_path = prince_path
        self._timeout_seconds = timeout_seconds
        self._preview_max_dpi = preview_max_dpi
        self._preview_max_bytes = preview_max_bytes
        self._staged_file_limit = staged_file_limit
        self._staged_bytes_limit = staged_bytes_limit

    async def generate_pdf(
        self,
        env: SandboxEnvironment,
        *,
        html_path: str,
        output_path: str,
    ) -> PdfGenerationResult:
        html_workspace_path = _workspace_path(html_path)
        source_dir = str(PurePosixPath(html_workspace_path).parent)
        input_name = PurePosixPath(html_workspace_path).name

        with tempfile.TemporaryDirectory(prefix="email-agent-prince-") as tmp:
            render_root = Path(tmp) / "workspace"
            render_root.mkdir()
            await _stage_tree(
                env,
                source_dir,
                render_root,
                max_files=self._staged_file_limit,
                max_bytes=self._staged_bytes_limit,
            )
            input_path = render_root / input_name
            output_tmp = Path(tmp) / "output.pdf"
            if not input_path.exists():
                raise FileNotFoundError(html_workspace_path)

            proc = await asyncio.to_thread(
                subprocess.run,
                [self._prince_path, str(input_path), "-o", str(output_tmp)],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Prince exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
                )
            pdf_bytes = output_tmp.read_bytes()

        await env.write_bytes(output_path, pdf_bytes)
        return PdfGenerationResult(
            pdf_path=output_path,
            size_bytes=len(pdf_bytes),
            stdout=str(proc.stdout),
            stderr=str(proc.stderr),
        )

    async def preview_pdf(
        self,
        env: SandboxEnvironment,
        *,
        pdf_path: str,
        page: int = 1,
        dpi: int = 160,
    ) -> PdfPreviewResult:
        if page < 1:
            raise ValueError("page must be 1 or greater")
        if dpi < 36:
            raise ValueError("dpi must be at least 36")
        dpi = min(dpi, self._preview_max_dpi)
        pdf_bytes = await env.read_bytes(pdf_path)
        png_bytes, page_count = await asyncio.to_thread(_render_pdf_page, pdf_bytes, page, dpi)
        if len(png_bytes) > self._preview_max_bytes:
            raise ValueError(
                f"preview PNG is too large ({len(png_bytes)} bytes > {self._preview_max_bytes})"
            )
        return PdfPreviewResult(
            pdf_path=pdf_path,
            page=page,
            page_count=page_count,
            dpi=dpi,
            png_bytes=png_bytes,
        )


async def _stage_tree(
    env: SandboxEnvironment,
    source_dir: str,
    target_dir: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    copied_files = 0
    copied_bytes = 0

    async def copy_dir(workspace_dir: str, local_dir: Path) -> None:
        nonlocal copied_files, copied_bytes
        await asyncio.to_thread(local_dir.mkdir, parents=True, exist_ok=True)
        for name in await env.readdir(workspace_dir):
            if name in {".", ".."}:
                continue
            workspace_child = str(PurePosixPath(workspace_dir) / name)
            stat = await env.stat(workspace_child)
            if stat.is_symlink:
                continue
            if stat.is_dir:
                await copy_dir(workspace_child, local_dir / name)
                continue
            if not stat.is_file:
                continue
            copied_files += 1
            copied_bytes += stat.size
            if copied_files > max_files:
                raise ValueError(f"too many files to stage for PDF render (>{max_files})")
            if copied_bytes > max_bytes:
                raise ValueError(f"too many bytes to stage for PDF render (>{max_bytes})")
            data = await env.read_bytes(workspace_child)
            destination = local_dir / name
            await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(destination.write_bytes, data)

    await copy_dir(source_dir, target_dir)


def _render_pdf_page(pdf_bytes: bytes, page: int, dpi: int) -> tuple[bytes, int]:
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is not installed; install the pymupdf package") from exc

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_count = len(doc)
        if page > page_count:
            raise ValueError(f"page {page} out of range; PDF has {page_count} page(s)")
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        return pix.tobytes("png"), page_count
    finally:
        doc.close()


def _workspace_path(path: str) -> str:
    if path.startswith(WORKSPACE_ROOT):
        return str(PurePosixPath(path))
    if path.startswith("/"):
        return str(PurePosixPath(f"{WORKSPACE_ROOT}{path}"))
    return str(PurePosixPath(WORKSPACE_ROOT) / PurePosixPath(path))


def prince_available(prince_path: str) -> bool:
    return shutil.which(prince_path) is not None
