import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import AssistantSurfaceRow
from email_agent.runtime.assistant_runtime import Accepted, Dropped

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


def make_template_surface_target(template: str) -> SurfaceTargetResolver:
    """Build a target resolver from an explicit URL template.

    Supported placeholders are `{assistant_id}` and `{port}`. A local-dev
    deployment can opt into `http://127.0.0.1:{port}`, but the app does not
    assume localhost is the assistant workspace target unless configured.
    """

    def resolve(settings: SurfaceProxySettings) -> str:
        return template.format(assistant_id=settings.assistant_id, port=settings.port)

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
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=_response_headers(upstream.headers),
            )
        except httpx.TimeoutException:
            status_code = 504
            return Response("Assistant surface timed out\n", status_code=status_code)
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

    router.add_api_route(
        "/surfaces/{assistant_id}/_action/run",
        run_surface_action,
        methods=["POST"],
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
        name: value for name, value in headers.items() if name.lower() not in HOP_BY_HOP_HEADERS
    }


__all__ = [
    "SurfaceProxySettings",
    "make_surfaces_router",
    "make_template_surface_target",
]
