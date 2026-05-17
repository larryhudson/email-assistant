from typing import Protocol

from email_agent.sandbox.environment import SandboxEnvironment


class DocumentToolsPort(Protocol):
    async def pandoc(
        self,
        env: SandboxEnvironment,
        *,
        args: list[str],
        input_paths: list[str],
        output_paths: list[str],
        timeout_s: int | None = None,
    ) -> str: ...

    async def soffice(
        self,
        env: SandboxEnvironment,
        *,
        args: list[str],
        input_paths: list[str],
        output_paths: list[str],
        timeout_s: int | None = None,
    ) -> str: ...

    async def python_docx(
        self,
        env: SandboxEnvironment,
        *,
        path: str,
        operations: list[dict],
        output_path: str | None = None,
    ) -> str: ...
