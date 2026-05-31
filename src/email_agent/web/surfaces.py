import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import AssistantSurfaceRow
from email_agent.runtime.assistant_runtime import Accepted, Dropped
from email_agent.web.surface_tokens import verify_surface_token

if TYPE_CHECKING:
    from email_agent.runtime.assistant_runtime import AssistantRuntime

log = logging.getLogger("email_agent.web.surfaces")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
RECALCULATED_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
}
BLOCKED_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
}


@dataclass(frozen=True)
class SurfaceProxySettings:
    assistant_id: str
    port: int


SurfaceTargetResolver = Callable[[SurfaceProxySettings], str]


class SurfaceTargetUnavailableError(RuntimeError):
    """Raised when a configured surface target cannot be resolved."""


def make_template_surface_target(template: str) -> SurfaceTargetResolver:
    """Build a target resolver from an explicit URL template.

    Supported placeholders are `{assistant_id}` and `{port}`. A local-dev
    deployment can opt into `http://127.0.0.1:{port}`, but the app does not
    assume localhost is the assistant workspace target unless configured.
    """

    def resolve(settings: SurfaceProxySettings) -> str:
        return template.format(assistant_id=settings.assistant_id, port=settings.port)

    return resolve


def make_docker_surface_target(
    client: Any,
    *,
    network: str | None = None,
    container_name_template: str = "email-agent-sandbox-{assistant_id}",
) -> SurfaceTargetResolver:
    """Build a target resolver by inspecting assistant workspace containers."""

    def resolve(settings: SurfaceProxySettings) -> str:
        container_name = container_name_template.format(assistant_id=settings.assistant_id)
        try:
            container = client.containers.get(container_name)
            container.reload()
        except Exception as exc:
            raise SurfaceTargetUnavailableError(
                "Assistant surface container is unreachable"
            ) from exc

        attrs = container.attrs or {}
        published_url = _docker_surface_published_url(attrs, settings.port)
        if published_url is not None:
            return published_url

        networks = attrs.get("NetworkSettings", {}).get("Networks", {})
        network_info = _docker_surface_network_info(networks, network)
        ip_address = network_info.get("IPAddress") if network_info is not None else None
        if not ip_address:
            raise SurfaceTargetUnavailableError("Assistant surface container has no reachable IP")
        return f"http://{ip_address}:{settings.port}"

    return resolve


def make_surfaces_router(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    runtime: "AssistantRuntime",
    target_resolver: SurfaceTargetResolver | None = None,
    request_timeout_seconds: float = 30.0,
) -> APIRouter:
    router = APIRouter()

    async def run_surface_action(request: Request, assistant_id: str) -> dict[str, str]:
        settings = await _load_surface_settings(session_factory, assistant_id)
        if settings is None:
            raise HTTPException(status_code=404, detail="Assistant surface not found")

        _require_same_origin_action_request(request)
        _require_json_content_type(request)
        payload = await _read_action_payload(request)
        provider_message_id = _provider_message_id(payload)
        subject = _action_subject(payload)
        body_text = _action_body_text(payload)
        message_id_header = _message_id_header(provider_message_id)

        outcome = await runtime.accept_surface_action(
            assistant_id=assistant_id,
            subject=subject,
            body_text=body_text,
            provider_message_id=provider_message_id,
            message_id_header=message_id_header,
        )
        if isinstance(outcome, Dropped):
            raise HTTPException(status_code=404, detail=outcome.detail)
        assert isinstance(outcome, Accepted)
        if outcome.run_id is None:
            raise HTTPException(status_code=500, detail="Surface action did not create a run")
        log.info(
            "surface action assistant=%s run=%s created=%s provider_message_id=%s",
            assistant_id,
            outcome.run_id,
            outcome.created,
            provider_message_id,
        )
        return {"run_id": outcome.run_id, "status": "queued"}

    async def proxy_surface(
        request: Request,
        assistant_id: str,
        path: str = "",
    ) -> Response:
        start = time.monotonic()
        auth_mode = "owner_basic"
        status_code = 502
        try:
            settings = await _load_surface_settings(session_factory, assistant_id)
            if settings is None:
                status_code = 404
                return Response("Assistant surface not found\n", status_code=status_code)

            bearer_token = _bearer_token(request)
            if bearer_token is not None:
                if not _is_api_surface_path(path):
                    status_code = 401
                    return Response(
                        "Bearer tokens are only valid for API surfaces\n",
                        status_code=status_code,
                    )
                if not await verify_surface_token(
                    session_factory,
                    assistant_id=assistant_id,
                    token=bearer_token,
                ):
                    status_code = 401
                    return Response("Invalid surface token\n", status_code=status_code)
                auth_mode = "api_token"

            if target_resolver is None:
                status_code = 503
                return Response(
                    "Assistant surface target is not configured\n",
                    status_code=status_code,
                )

            request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
            target_base = target_resolver(settings).rstrip("/")
            target_url = f"{target_base}/{path.lstrip('/')}"
            if request.url.query:
                target_url = f"{target_url}?{request.url.query}"

            body = await request.body()
            headers = _forward_headers(request.headers, assistant_id, auth_mode, request_id)

            async with httpx.AsyncClient(timeout=request_timeout_seconds) as client:
                upstream = await client.request(
                    request.method,
                    target_url,
                    content=body,
                    headers=headers,
                )
            status_code = upstream.status_code
            response_content = _rewrite_html_response(
                upstream.content,
                upstream.headers,
                assistant_id,
            )
            return Response(
                content=response_content,
                status_code=upstream.status_code,
                headers=_response_headers(upstream.headers),
            )
        except httpx.TimeoutException:
            status_code = 504
            return Response("Assistant surface timed out\n", status_code=status_code)
        except httpx.ConnectError as exc:
            status_code = 502
            if _is_connection_refused(exc):
                return Response(
                    "Assistant surface server is not listening\n",
                    status_code=status_code,
                )
            return Response("Assistant surface target unreachable\n", status_code=status_code)
        except SurfaceTargetUnavailableError:
            status_code = 502
            return Response("Assistant surface target unreachable\n", status_code=status_code)
        except httpx.HTTPError:
            log.exception("assistant surface proxy failed")
            status_code = 502
            return Response("Assistant surface unavailable\n", status_code=status_code)
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.info(
                "surface request assistant=%s method=%s path=/%s auth=%s status=%d duration_ms=%d",
                assistant_id,
                request.method,
                path,
                auth_mode,
                status_code,
                duration_ms,
            )

    async def check_surface(assistant_id: str) -> dict[str, str | int]:
        settings = await _load_surface_settings(session_factory, assistant_id)
        if settings is None:
            raise HTTPException(status_code=404, detail="Assistant surface not enabled")
        if target_resolver is None:
            raise HTTPException(
                status_code=503, detail="Assistant surface target is not configured"
            )

        try:
            target_base = target_resolver(settings).rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                upstream = await client.get(target_base)
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="Assistant surface timed out") from exc
        except httpx.ConnectError as exc:
            if _is_connection_refused(exc):
                raise HTTPException(
                    status_code=502,
                    detail="Assistant surface server is not listening",
                ) from exc
            raise HTTPException(
                status_code=502,
                detail="Assistant surface target unreachable",
            ) from exc
        except SurfaceTargetUnavailableError as exc:
            raise HTTPException(
                status_code=502,
                detail="Assistant surface target unreachable",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Assistant surface unavailable") from exc

        return {
            "assistant_id": assistant_id,
            "status": "reachable",
            "target": target_base,
            "upstream_status": upstream.status_code,
        }

    router.add_api_route(
        "/surfaces/{assistant_id}/_action/run",
        run_surface_action,
        methods=["POST"],
    )
    router.add_api_route(
        "/surfaces/{assistant_id}/_check",
        check_surface,
        methods=["GET"],
    )
    router.add_api_route(
        "/surfaces/{assistant_id}",
        proxy_surface,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    router.add_api_route(
        "/surfaces/{assistant_id}/{path:path}",
        proxy_surface,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    return router


def _require_same_origin_action_request(request: Request) -> None:
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site is not None and sec_fetch_site.lower() == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-origin surface action rejected")

    origin = request.headers.get("origin")
    if origin is None:
        return

    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin != expected_origin:
        raise HTTPException(status_code=403, detail="Cross-origin surface action rejected")


def _require_json_content_type(request: Request) -> None:
    content_type = request.headers.get("content-type")
    if content_type is None:
        raise HTTPException(status_code=415, detail="Expected application/json content type")

    media_type = content_type.partition(";")[0].strip().lower()
    if media_type == "application/json":
        return
    if media_type.startswith("application/") and media_type.endswith("+json"):
        return
    raise HTTPException(status_code=415, detail="Expected application/json content type")


async def _read_action_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Expected JSON action payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Expected JSON object action payload")
    return payload


def _provider_message_id(payload: dict[str, Any]) -> str:
    raw = payload.get("idempotency_key")
    if raw is None:
        return f"surface-{uuid.uuid4().hex}"
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422, detail="idempotency_key must be a non-empty string")
    return raw


def _action_subject(payload: dict[str, Any]) -> str:
    raw = payload.get("subject")
    if raw is None:
        return "Surface action"
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422, detail="subject must be a non-empty string")
    return raw


def _action_body_text(payload: dict[str, Any]) -> str:
    raw = payload.get("body_text")
    if raw is not None:
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail="body_text must be a string")
        return raw
    import json

    return (
        "Surface action payload\n\n"
        f"Received at: {datetime.now(UTC).isoformat()}\n\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}"
    )


def _message_id_header(provider_message_id: str) -> str:
    safe = uuid.uuid5(uuid.NAMESPACE_URL, provider_message_id).hex[:16]
    return f"<surface-{safe}@email-agent>"


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _is_api_surface_path(path: str) -> bool:
    return path == "api" or path.startswith("api/")


def _is_connection_refused(exc: BaseException) -> bool:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if getattr(current, "errno", None) in {61, 111}:
            return True
        text = str(current).lower()
        if "connection refused" in text or "connect call failed" in text:
            return True
        for nested in (*getattr(current, "args", ()), current.__cause__, current.__context__):
            if isinstance(nested, BaseException):
                stack.append(nested)
    return False


def _docker_surface_network_info(
    networks: dict[str, Any],
    network: str | None,
) -> dict[str, Any] | None:
    if network is not None:
        info = networks.get(network)
        return info if isinstance(info, dict) else None
    for info in networks.values():
        if isinstance(info, dict) and info.get("IPAddress"):
            return info
    return None


def _docker_surface_published_url(attrs: dict[str, Any], port: int) -> str | None:
    bindings = attrs.get("NetworkSettings", {}).get("Ports", {}).get(f"{port}/tcp")
    if not bindings:
        return None
    binding = bindings[0]
    if not isinstance(binding, dict):
        return None
    host_port = binding.get("HostPort")
    if not host_port:
        return None
    host_ip = binding.get("HostIp") or "127.0.0.1"
    if host_ip in {"0.0.0.0", "::"}:
        host_ip = "127.0.0.1"
    if ":" in host_ip and not host_ip.startswith("["):
        host_ip = f"[{host_ip}]"
    return f"http://{host_ip}:{host_port}"


def _rewrite_html_response(
    content: bytes,
    headers,
    assistant_id: str,
) -> bytes:
    content_type = headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "text/html":
        return content
    encoding = _html_charset(content_type)
    try:
        html = content.decode(encoding)
    except UnicodeDecodeError:
        return content
    rewritten = _rewrite_html_root_relative_urls(html, assistant_id=assistant_id)
    return rewritten.encode(encoding)


def _html_charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value.strip():
            return value.strip().strip('"')
    return "utf-8"


class _SurfaceHtmlRewriter(HTMLParser):
    _REWRITE_ATTRS: ClassVar[dict[str, set[str]]] = {
        "a": {"href"},
        "form": {"action"},
        "img": {"src"},
        "link": {"href"},
        "script": {"src"},
    }

    def __init__(self, *, assistant_id: str) -> None:
        super().__init__(convert_charrefs=False)
        self._prefix = f"/surfaces/{assistant_id}"
        self._parts: list[str] = []

    def rewritten(self) -> str:
        return "".join(self._parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append_starttag(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append_starttag(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self._parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self._parts.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self._parts.append(f"<![{data}]>")

    def _append_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        closed: bool,
    ) -> None:
        rewritten_attrs = [(name, self._rewrite_attr(tag, name, value)) for name, value in attrs]
        rendered_attrs = "".join(_render_html_attr(name, value) for name, value in rewritten_attrs)
        closing = " /" if closed else ""
        self._parts.append(f"<{tag}{rendered_attrs}{closing}>")

    def _rewrite_attr(self, tag: str, name: str, value: str | None) -> str | None:
        if value is None:
            return None
        if name.lower() not in self._REWRITE_ATTRS.get(tag.lower(), set()):
            return value
        if not _is_rewritable_root_relative_url(value):
            return value
        if value == self._prefix or value.startswith(f"{self._prefix}/"):
            return value
        return f"{self._prefix}{value}"


def _rewrite_html_root_relative_urls(html: str, *, assistant_id: str) -> str:
    parser = _SurfaceHtmlRewriter(assistant_id=assistant_id)
    parser.feed(html)
    parser.close()
    return parser.rewritten()


def _render_html_attr(name: str, value: str | None) -> str:
    if value is None:
        return f" {name}"
    return f' {name}="{escape(value, quote=True)}"'


def _is_rewritable_root_relative_url(value: str) -> bool:
    return value.startswith("/") and not value.startswith("//")


async def _load_surface_settings(
    session_factory: async_sessionmaker[AsyncSession],
    assistant_id: str,
) -> SurfaceProxySettings | None:
    async with session_factory() as session:
        row = await session.get(AssistantSurfaceRow, assistant_id)
        if row is None or not row.enabled:
            return None
        return SurfaceProxySettings(assistant_id=assistant_id, port=row.port)


def _forward_headers(
    headers,
    assistant_id: str,
    auth_mode: str,
    request_id: str,
) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for name, value in headers.items():
        lower_name = name.lower()
        if lower_name in HOP_BY_HOP_HEADERS or lower_name in BLOCKED_REQUEST_HEADERS:
            continue
        if lower_name.startswith("x-assistant-"):
            continue
        if lower_name.startswith("x-viewer-"):
            continue
        if lower_name.startswith("x-surface-"):
            continue
        forwarded[name] = value

    forwarded["X-Assistant-Id"] = assistant_id
    forwarded["X-Surface-Auth"] = auth_mode
    forwarded["X-Surface-Request-Id"] = request_id
    return forwarded


def _response_headers(headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
        and name.lower() not in RECALCULATED_RESPONSE_HEADERS
    }


__all__ = [
    "SurfaceProxySettings",
    "SurfaceTargetUnavailableError",
    "make_docker_surface_target",
    "make_surfaces_router",
    "make_template_surface_target",
]
