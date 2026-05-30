import json
import logging
import secrets
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import AgentRun
from email_agent.runtime.assistant_runtime import Accepted, AssistantRuntime, Dropped

log = logging.getLogger("email_agent.web.assistant_tools")


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = "assistant_tools_run"
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class RunCreateResponse(BaseModel):
    run_id: str
    status: str


class RunGetResponse(BaseModel):
    run_id: str
    assistant_id: str
    status: str
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class EventLogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    detail: dict[str, Any] = Field(default_factory=dict)


class EventLogResponse(BaseModel):
    status: str


def make_assistant_tools_router(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    runtime: AssistantRuntime,
    shared_token: str | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/_internal/assistant-tools")

    @router.get("/openapi.json")
    async def assistant_tools_openapi() -> dict[str, Any]:
        return _openapi_spec()

    @router.post("/v1/runs", response_model=RunCreateResponse)
    async def create_run(
        request: Request,
        payload: RunCreateRequest,
        assistant_id: str = Header(alias="X-Assistant-Id"),
    ) -> RunCreateResponse:
        _require_internal_access(request, shared_token)
        provider_message_id = payload.idempotency_key or f"assistant-tools-{uuid.uuid4().hex}"
        body_text = _run_body_text(payload)
        outcome = await runtime.accept_surface_action(
            assistant_id=assistant_id,
            subject=f"Assistant tool: {payload.reason}",
            body_text=body_text,
            provider_message_id=provider_message_id,
            message_id_header=_message_id_header(provider_message_id),
        )
        if isinstance(outcome, Dropped):
            raise HTTPException(status_code=404, detail=outcome.detail)
        assert isinstance(outcome, Accepted)
        if outcome.run_id is None:
            raise HTTPException(status_code=500, detail="Run was not queued")
        return RunCreateResponse(run_id=outcome.run_id, status="queued")

    @router.get("/v1/runs/{run_id}", response_model=RunGetResponse)
    async def get_run(
        request: Request,
        run_id: str,
        assistant_id: str = Header(alias="X-Assistant-Id"),
    ) -> RunGetResponse:
        _require_internal_access(request, shared_token)
        async with session_factory() as session:
            run = await session.get(AgentRun, run_id)
        if run is None or run.assistant_id != assistant_id:
            raise HTTPException(status_code=404, detail="Run not found")
        return RunGetResponse(
            run_id=run.id,
            assistant_id=run.assistant_id,
            status=run.status,
            error=run.error,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )

    @router.post("/v1/events", response_model=EventLogResponse)
    async def log_event(
        request: Request,
        payload: EventLogRequest,
        assistant_id: str = Header(alias="X-Assistant-Id"),
    ) -> EventLogResponse:
        _require_internal_access(request, shared_token)
        log.info(
            "assistant tool event assistant=%s event=%s detail=%s",
            assistant_id,
            payload.event,
            payload.detail,
        )
        return EventLogResponse(status="logged")

    return router


def _require_internal_access(request: Request, shared_token: str | None) -> None:
    if shared_token is None:
        return
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Assistant Tools token required")
    if not secrets.compare_digest(token, shared_token):
        raise HTTPException(status_code=403, detail="Assistant Tools token rejected")


def _run_body_text(payload: RunCreateRequest) -> str:
    return (
        "Assistant Tools API run request\n\n"
        f"Reason: {payload.reason}\n\n"
        "Input:\n"
        f"{json.dumps(payload.input, indent=2, sort_keys=True)}"
    )


def _message_id_header(provider_message_id: str) -> str:
    safe = uuid.uuid5(uuid.NAMESPACE_URL, provider_message_id).hex[:16]
    return f"<assistant-tools-{safe}@email-agent>"


def _openapi_spec() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Assistant Tools API",
            "version": "0.1.0",
        },
        "servers": [{"url": "/_internal/assistant-tools"}],
        "paths": {
            "/v1/runs": {
                "post": {
                    "operationId": "runs.create",
                    "parameters": [_assistant_id_header_spec()],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "reason": {"type": "string"},
                                        "input": {"type": "object"},
                                        "idempotency_key": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Queued run"}},
                }
            },
            "/v1/runs/{run_id}": {
                "get": {
                    "operationId": "runs.get",
                    "parameters": [
                        _assistant_id_header_spec(),
                        {
                            "name": "run_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Run status"}},
                }
            },
            "/v1/events": {
                "post": {
                    "operationId": "events.log",
                    "parameters": [_assistant_id_header_spec()],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["event"],
                                    "properties": {
                                        "event": {"type": "string"},
                                        "detail": {"type": "object"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Logged event"}},
                }
            },
        },
    }


def _assistant_id_header_spec() -> dict[str, Any]:
    return {
        "name": "X-Assistant-Id",
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
    }


__all__ = ["make_assistant_tools_router"]
