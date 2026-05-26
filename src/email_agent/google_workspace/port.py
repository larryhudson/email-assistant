from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

GOOGLE_WORKSPACE_CREDENTIAL_KEY = "google_workspace"
GOOGLE_WORKSPACE_USER_CREDENTIAL_KIND = "google_authorized_user_file"
GOOGLE_CALENDAR_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar",)


class GoogleCalendarError(RuntimeError):
    """Safe-to-surface Calendar operation failure."""


class GoogleCalendarCredentialError(GoogleCalendarError):
    """Missing, invalid, or unusable Google Workspace credential."""


class GoogleCalendarListResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[dict[str, Any]] = Field(default_factory=list)


class GoogleCalendarEventsResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[dict[str, Any]] = Field(default_factory=list)


class GoogleCalendarEventResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    status: str | None = None
    html_link: str | None = Field(default=None, alias="htmlLink")
    start: dict[str, Any] | None = None
    end: dict[str, Any] | None = None
    attendees: list[dict[str, Any]] | None = None


class GoogleCalendarFreeBusyResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    calendars: dict[str, Any] = Field(default_factory=dict)
    groups: dict[str, Any] | None = None


class GoogleCalendarDeleteResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    deleted: bool
    calendar_id: str
    event_id: str


class GoogleCalendarPort(Protocol):
    async def list_calendars(self, assistant_id: str) -> GoogleCalendarListResult: ...

    async def list_events(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        query: str | None = None,
        max_results: int = 50,
    ) -> GoogleCalendarEventsResult: ...

    async def get_event(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        event_id: str,
    ) -> GoogleCalendarEventResult: ...

    async def check_free_busy(
        self,
        assistant_id: str,
        *,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
    ) -> GoogleCalendarFreeBusyResult: ...

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
    ) -> GoogleCalendarEventResult: ...

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
    ) -> GoogleCalendarEventResult: ...

    async def delete_event(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        event_id: str,
    ) -> GoogleCalendarDeleteResult: ...


__all__ = [
    "GOOGLE_CALENDAR_SCOPES",
    "GOOGLE_WORKSPACE_CREDENTIAL_KEY",
    "GOOGLE_WORKSPACE_USER_CREDENTIAL_KIND",
    "GoogleCalendarCredentialError",
    "GoogleCalendarDeleteResult",
    "GoogleCalendarError",
    "GoogleCalendarEventResult",
    "GoogleCalendarEventsResult",
    "GoogleCalendarFreeBusyResult",
    "GoogleCalendarListResult",
    "GoogleCalendarPort",
]
