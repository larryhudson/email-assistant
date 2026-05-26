import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from email_agent.google_workspace.port import (
    GOOGLE_CALENDAR_SCOPES,
    GOOGLE_WORKSPACE_CREDENTIAL_KEY,
    GOOGLE_WORKSPACE_USER_CREDENTIAL_KIND,
    GoogleCalendarCredentialError,
    GoogleCalendarDeleteResult,
    GoogleCalendarError,
    GoogleCalendarEventResult,
    GoogleCalendarEventsResult,
    GoogleCalendarFreeBusyResult,
    GoogleCalendarListResult,
)
from email_agent.tool_credentials.port import ActiveToolCredential, ToolCredentialResolver

CredentialLoader = Callable[[Path, tuple[str, ...]], Any]
RequestFactory = Callable[[], Any]
ServiceBuilder = Callable[[Any], Any]


class GoogleWorkspaceCalendarAdapter:
    """Google Calendar adapter backed by official Google Python client libraries."""

    def __init__(
        self,
        *,
        credential_resolver: ToolCredentialResolver,
        credential_root: Path | None = None,
        credential_loader: CredentialLoader | None = None,
        request_factory: RequestFactory | None = None,
        service_builder: ServiceBuilder | None = None,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._credential_root = credential_root
        self._credential_loader = credential_loader or _load_authorized_user_credentials
        self._request_factory = request_factory or _default_request
        self._service_builder = service_builder or _default_calendar_service

    async def list_calendars(self, assistant_id: str) -> GoogleCalendarListResult:
        service = await self._service_for(assistant_id)
        result = await asyncio.to_thread(service.calendarList().list().execute)
        return GoogleCalendarListResult.model_validate(result)

    async def list_events(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        query: str | None = None,
        max_results: int = 50,
    ) -> GoogleCalendarEventsResult:
        _assert_aware("time_min", time_min)
        _assert_aware("time_max", time_max)
        service = await self._service_for(assistant_id)
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max(1, min(max_results, 250)),
        }
        if query:
            params["q"] = query
        result = await asyncio.to_thread(service.events().list(**params).execute)
        return GoogleCalendarEventsResult.model_validate(result)

    async def get_event(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        event_id: str,
    ) -> GoogleCalendarEventResult:
        service = await self._service_for(assistant_id)
        result = await asyncio.to_thread(
            service.events().get(calendarId=calendar_id, eventId=event_id).execute
        )
        return GoogleCalendarEventResult.model_validate(result)

    async def check_free_busy(
        self,
        assistant_id: str,
        *,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
    ) -> GoogleCalendarFreeBusyResult:
        _assert_aware("time_min", time_min)
        _assert_aware("time_max", time_max)
        service = await self._service_for(assistant_id)
        body = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "items": [{"id": calendar_id} for calendar_id in calendar_ids],
        }
        result = await asyncio.to_thread(service.freebusy().query(body=body).execute)
        return GoogleCalendarFreeBusyResult.model_validate(result)

    async def create_event(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        summary: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> GoogleCalendarEventResult:
        _assert_aware("start", start)
        _assert_aware("end", end)
        service = await self._service_for(assistant_id)
        body = _event_body(
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
        )
        result = await asyncio.to_thread(
            service.events().insert(calendarId=calendar_id, body=body).execute
        )
        return GoogleCalendarEventResult.model_validate(result)

    async def update_event(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        event_id: str,
        summary: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> GoogleCalendarEventResult:
        if start is not None:
            _assert_aware("start", start)
        if end is not None:
            _assert_aware("end", end)
        if (start is None) != (end is None):
            raise GoogleCalendarError("start and end must be provided together")
        service = await self._service_for(assistant_id)
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if start is not None and end is not None:
            body["start"] = {"dateTime": start.isoformat()}
            body["end"] = {"dateTime": end.isoformat()}
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if attendees is not None:
            body["attendees"] = [{"email": email} for email in attendees]
        if not body:
            raise GoogleCalendarError("at least one event field must be provided")

        result = await asyncio.to_thread(
            service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute
        )
        return GoogleCalendarEventResult.model_validate(result)

    async def delete_event(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        event_id: str,
    ) -> GoogleCalendarDeleteResult:
        service = await self._service_for(assistant_id)
        await asyncio.to_thread(
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute
        )
        return GoogleCalendarDeleteResult(
            deleted=True,
            calendar_id=calendar_id,
            event_id=event_id,
        )

    async def _service_for(self, assistant_id: str) -> Any:
        credential = await self._resolve_credential(assistant_id)
        path = self._credential_path(credential.secret_ref)
        try:
            creds = await asyncio.to_thread(self._credential_loader, path, GOOGLE_CALENDAR_SCOPES)
            refreshed = False
            if not getattr(creds, "valid", False):
                if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
                    await asyncio.to_thread(creds.refresh, self._request_factory())
                    refreshed = True
                else:
                    raise GoogleCalendarCredentialError(
                        "Google Workspace credential is invalid and cannot be refreshed"
                    )
            if refreshed:
                await asyncio.to_thread(path.write_text, creds.to_json())
            return await asyncio.to_thread(self._service_builder, creds)
        except GoogleCalendarError:
            raise
        except FileNotFoundError as exc:
            raise GoogleCalendarCredentialError(
                "Google Workspace credential file was not found"
            ) from exc
        except Exception as exc:
            raise GoogleCalendarError(_redact(str(exc), path)) from exc

    async def _resolve_credential(self, assistant_id: str) -> ActiveToolCredential:
        credential = await self._credential_resolver.get_active(
            assistant_id, GOOGLE_WORKSPACE_CREDENTIAL_KEY
        )
        if credential is None:
            raise GoogleCalendarCredentialError("Google Workspace is not linked for this assistant")
        if credential.credential_kind != GOOGLE_WORKSPACE_USER_CREDENTIAL_KIND:
            raise GoogleCalendarCredentialError(
                f"Google Workspace credential kind {credential.credential_kind!r} is not supported"
            )
        return credential

    def _credential_path(self, secret_ref: str) -> Path:
        if not secret_ref.startswith("file:"):
            raise GoogleCalendarCredentialError("Google Workspace credential must use a file: ref")
        raw = secret_ref.removeprefix("file:")
        if not raw:
            raise GoogleCalendarCredentialError("Google Workspace credential file ref is empty")
        path = Path(raw).expanduser()
        if not path.is_absolute() and self._credential_root is not None:
            path = self._credential_root / path
        return path


def _event_body(
    *,
    summary: str,
    start: datetime,
    end: datetime,
    description: str | None,
    location: str | None,
    attendees: list[str] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if attendees is not None:
        body["attendees"] = [{"email": email} for email in attendees]
    return body


def _assert_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GoogleCalendarError(f"{name} must be timezone-aware")


def _redact(message: str, path: Path | None = None) -> str:
    redacted = message
    if path is not None:
        redacted = redacted.replace(str(path), "<credential-file>")
    return re.sub(
        r"(?i)(refresh_token|access_token|client_secret|token)['\"]?\s*[:=]\s*['\"]?[^,'\"\s}]+",
        r"\1=<redacted>",
        redacted,
    )


def _load_authorized_user_credentials(path: Path, scopes: tuple[str, ...]) -> Any:
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_file(str(path), scopes)


def _default_request() -> Any:
    from google.auth.transport.requests import Request

    return Request()


def _default_calendar_service(creds: Any) -> Any:
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


__all__ = ["GoogleWorkspaceCalendarAdapter"]
