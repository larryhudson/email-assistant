from datetime import UTC, datetime
from pathlib import Path

import pytest

from email_agent.google_workspace.calendar import GoogleWorkspaceCalendarAdapter
from email_agent.google_workspace.port import (
    GOOGLE_WORKSPACE_CREDENTIAL_KEY,
    GOOGLE_WORKSPACE_USER_CREDENTIAL_KIND,
    GoogleCalendarCredentialError,
    GoogleCalendarError,
)
from email_agent.tool_credentials import ActiveToolCredential, ToolCredentialStatus


class _Resolver:
    def __init__(self, credential: ActiveToolCredential | None) -> None:
        self.credential = credential
        self.calls: list[tuple[str, str]] = []

    async def get_active(
        self, assistant_id: str, tool_credential_key: str
    ) -> ActiveToolCredential | None:
        self.calls.append((assistant_id, tool_credential_key))
        return self.credential


class _Creds:
    def __init__(self, *, valid: bool = True, expired: bool = False, refresh_token: str = "rt"):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False

    def refresh(self, request) -> None:
        _ = request
        self.valid = True
        self.expired = False
        self.refreshed = True

    def to_json(self) -> str:
        return '{"token":"new-token","refresh_token":"new-refresh"}'


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _CalendarList:
    def __init__(self, service: "_Service") -> None:
        self.service = service

    def list(self):
        self.service.calls.append(("calendarList.list", {}))
        return _Execute({"items": [{"id": "primary"}]})


class _Events:
    def __init__(self, service: "_Service") -> None:
        self.service = service

    def list(self, **kwargs):
        self.service.calls.append(("events.list", kwargs))
        return _Execute({"items": [{"id": "event-1"}]})

    def get(self, **kwargs):
        self.service.calls.append(("events.get", kwargs))
        return _Execute({"id": kwargs["eventId"]})

    def insert(self, **kwargs):
        self.service.calls.append(("events.insert", kwargs))
        return _Execute({"id": "created", **kwargs["body"]})

    def patch(self, **kwargs):
        self.service.calls.append(("events.patch", kwargs))
        return _Execute({"id": kwargs["eventId"], **kwargs["body"]})

    def delete(self, **kwargs):
        self.service.calls.append(("events.delete", kwargs))
        return _Execute({})


class _FreeBusy:
    def __init__(self, service: "_Service") -> None:
        self.service = service

    def query(self, **kwargs):
        self.service.calls.append(("freebusy.query", kwargs))
        return _Execute({"calendars": {"primary": {"busy": []}}})


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def calendarList(self):
        return _CalendarList(self)

    def events(self):
        return _Events(self)

    def freebusy(self):
        return _FreeBusy(self)


def _credential(secret_ref: str = "file:token.json") -> ActiveToolCredential:
    return ActiveToolCredential(
        id="tc-1",
        assistant_id="a-1",
        tool_credential_key=GOOGLE_WORKSPACE_CREDENTIAL_KEY,
        label="Google Workspace",
        account_identifier="larry.hudson.assistant@gmail.com",
        credential_kind=GOOGLE_WORKSPACE_USER_CREDENTIAL_KIND,
        secret_ref=secret_ref,
        metadata={"scopes": ["calendar"]},
        status=ToolCredentialStatus.ACTIVE,
        last_verified_at=None,
    )


def _adapter(
    *,
    tmp_path: Path,
    credential: ActiveToolCredential | None = None,
    creds: _Creds | None = None,
    service: _Service | None = None,
) -> tuple[GoogleWorkspaceCalendarAdapter, _Resolver, _Creds, _Service]:
    actual_creds = creds or _Creds()
    actual_service = service or _Service()
    resolver = _Resolver(credential if credential is not None else _credential())

    def load(path: Path, scopes: tuple[str, ...]):
        assert path == tmp_path / "token.json"
        assert scopes == ("https://www.googleapis.com/auth/calendar",)
        return actual_creds

    adapter = GoogleWorkspaceCalendarAdapter(
        credential_resolver=resolver,
        credential_root=tmp_path,
        credential_loader=load,
        request_factory=lambda: object(),
        service_builder=lambda loaded_creds: actual_service,
    )
    return adapter, resolver, actual_creds, actual_service


async def test_list_calendars_resolves_google_workspace_credential(tmp_path: Path) -> None:
    (tmp_path / "token.json").write_text("{}")
    adapter, resolver, _creds, _service = _adapter(tmp_path=tmp_path)

    result = await adapter.list_calendars("a-1")

    assert result.items == [{"id": "primary"}]
    assert resolver.calls == [("a-1", GOOGLE_WORKSPACE_CREDENTIAL_KEY)]


async def test_list_events_requires_timezone_aware_bounds(tmp_path: Path) -> None:
    adapter, _resolver, _creds, _service = _adapter(tmp_path=tmp_path)

    with pytest.raises(GoogleCalendarError, match="time_min must be timezone-aware"):
        await adapter.list_events(
            "a-1",
            calendar_id="primary",
            time_min=datetime(2026, 5, 26, 9, 0),
            time_max=datetime(2026, 5, 26, 10, 0, tzinfo=UTC),
        )


async def test_list_events_passes_expected_query(tmp_path: Path) -> None:
    adapter, _resolver, _creds, service = _adapter(tmp_path=tmp_path)

    await adapter.list_events(
        "a-1",
        calendar_id="primary",
        time_min=datetime(2026, 5, 26, 9, 0, tzinfo=UTC),
        time_max=datetime(2026, 5, 26, 10, 0, tzinfo=UTC),
        query="coffee",
        max_results=999,
    )

    assert service.calls == [
        (
            "events.list",
            {
                "calendarId": "primary",
                "timeMin": "2026-05-26T09:00:00+00:00",
                "timeMax": "2026-05-26T10:00:00+00:00",
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 250,
                "q": "coffee",
            },
        )
    ]


async def test_create_update_delete_and_freebusy_call_google_service(tmp_path: Path) -> None:
    adapter, _resolver, _creds, service = _adapter(tmp_path=tmp_path)
    start = datetime(2026, 5, 26, 9, 0, tzinfo=UTC)
    end = datetime(2026, 5, 26, 10, 0, tzinfo=UTC)

    created = await adapter.create_event(
        "a-1",
        calendar_id="primary",
        summary="Meet",
        start=start,
        end=end,
        attendees=["a@example.com"],
    )
    updated = await adapter.update_event(
        "a-1",
        calendar_id="primary",
        event_id="event-1",
        location="Office",
    )
    freebusy = await adapter.check_free_busy(
        "a-1",
        calendar_ids=["primary"],
        time_min=start,
        time_max=end,
    )
    deleted = await adapter.delete_event("a-1", calendar_id="primary", event_id="event-1")

    assert created.id == "created"
    assert updated.id == "event-1"
    assert updated.location == "Office"
    assert freebusy.calendars == {"primary": {"busy": []}}
    assert deleted.deleted is True
    assert deleted.calendar_id == "primary"
    assert deleted.event_id == "event-1"
    assert [name for name, _ in service.calls] == [
        "events.insert",
        "events.patch",
        "freebusy.query",
        "events.delete",
    ]


async def test_refreshes_and_persists_expired_credentials(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    token.write_text("{}")
    creds = _Creds(valid=False, expired=True, refresh_token="refresh")
    adapter, _resolver, actual_creds, _service = _adapter(tmp_path=tmp_path, creds=creds)

    await adapter.list_calendars("a-1")

    assert actual_creds.refreshed is True
    assert token.read_text() == '{"token":"new-token","refresh_token":"new-refresh"}'


async def test_missing_or_wrong_credentials_return_safe_errors(tmp_path: Path) -> None:
    adapter, _resolver, _creds, _service = _adapter(tmp_path=tmp_path, credential=None)
    adapter._credential_resolver = _Resolver(None)

    with pytest.raises(GoogleCalendarCredentialError, match="not linked"):
        await adapter.list_calendars("a-1")

    wrong = _credential()
    wrong = wrong.model_copy(update={"credential_kind": "api_token"})
    adapter, _resolver, _creds, _service = _adapter(tmp_path=tmp_path, credential=wrong)

    with pytest.raises(GoogleCalendarCredentialError, match="not supported"):
        await adapter.list_calendars("a-1")


async def test_redacts_credential_path_from_errors(tmp_path: Path) -> None:
    credential = _credential("file:secret-token.json")

    def fail(path: Path, scopes: tuple[str, ...]):
        _ = scopes
        raise ValueError(f"failed reading {path} refresh_token:abc123")

    adapter = GoogleWorkspaceCalendarAdapter(
        credential_resolver=_Resolver(credential),
        credential_root=tmp_path,
        credential_loader=fail,
        request_factory=lambda: object(),
        service_builder=lambda creds: _Service(),
    )

    with pytest.raises(GoogleCalendarError) as exc:
        await adapter.list_calendars("a-1")

    message = str(exc.value)
    assert str(tmp_path) not in message
    assert "abc123" not in message
    assert "<credential-file>" in message
