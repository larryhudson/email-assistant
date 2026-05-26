from datetime import datetime
from typing import Protocol

GOOGLE_WORKSPACE_CREDENTIAL_KEY = "google_workspace"
GOOGLE_WORKSPACE_USER_CREDENTIAL_KIND = "google_authorized_user_file"
GOOGLE_CALENDAR_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar",)


class GoogleCalendarError(RuntimeError):
    """Safe-to-surface Calendar operation failure."""


class GoogleCalendarCredentialError(GoogleCalendarError):
    """Missing, invalid, or unusable Google Workspace credential."""


class GoogleCalendarPort(Protocol):
    async def list_calendars(self, assistant_id: str) -> str: ...

    async def list_events(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        query: str | None = None,
        max_results: int = 50,
    ) -> str: ...

    async def get_event(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        event_id: str,
    ) -> str: ...

    async def check_free_busy(
        self,
        assistant_id: str,
        *,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
    ) -> str: ...

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
    ) -> str: ...

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
    ) -> str: ...

    async def delete_event(
        self,
        assistant_id: str,
        *,
        calendar_id: str,
        event_id: str,
    ) -> str: ...


__all__ = [
    "GOOGLE_CALENDAR_SCOPES",
    "GOOGLE_WORKSPACE_CREDENTIAL_KEY",
    "GOOGLE_WORKSPACE_USER_CREDENTIAL_KIND",
    "GoogleCalendarCredentialError",
    "GoogleCalendarError",
    "GoogleCalendarPort",
]
