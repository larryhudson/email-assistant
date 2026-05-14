from dataclasses import dataclass
from typing import Protocol

from email_agent.sandbox.environment import SandboxEnvironment


@dataclass(frozen=True)
class PdfGenerationResult:
    pdf_path: str
    size_bytes: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class PdfPreviewResult:
    pdf_path: str
    page: int
    page_count: int
    dpi: int
    png_bytes: bytes


class PdfRenderPort(Protocol):
    async def generate_pdf(
        self,
        env: SandboxEnvironment,
        *,
        html_path: str,
        output_path: str,
    ) -> PdfGenerationResult: ...

    async def preview_pdf(
        self,
        env: SandboxEnvironment,
        *,
        pdf_path: str,
        page: int = 1,
        dpi: int = 160,
    ) -> PdfPreviewResult: ...
