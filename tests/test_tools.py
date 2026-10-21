"""The tool layer: what reaches the CalDAV service, and what must not.

The service itself talks to a real server and is not exercised here. These pin
the decisions the tools make on top of it — which calendar and which URI they
accept, and what "today" means.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import CALENDAR
from mcp.server.mcpserver.exceptions import ToolError

import server as server_module

TASK = f"{CALENDAR}ABC.ics"


def test_all_tools_are_registered():
    import asyncio

    names = {t.name for t in asyncio.run(server_module.mcp.list_tools())}
    assert names == {
        "list_calendars", "set_calendar", "list_tasks", "create_task",
        "update_task", "complete_task", "catchup",
    }


def test_reading_tools_are_marked_read_only_and_complete_destructive():
    """Clients use these to decide what may run without asking. Completing a
    task deletes it from the server, so it is marked like a delete."""
    import asyncio

    tools = {t.name: t.annotations for t in asyncio.run(server_module.mcp.list_tools())}
    read_only = {name for name, a in tools.items() if a and a.read_only_hint}
    assert read_only == {"list_calendars", "list_tasks"}
    assert tools["complete_task"].destructive_hint is True


# ── task URIs ────────────────────────────────────────────────────────────────
#
# The CalDAV client sends the account's credentials to whatever URL it is given,
# and the URL is a tool argument — so it comes from the model.

def test_a_uri_from_the_calendar_is_passed_through(service):
    server_module.complete_task(TASK)
    assert service.deleted == [TASK]


def test_default_port_spelling_is_the_same_server(service):
    server_module.complete_task("https://dav.test:443/caldav/tasks/ABC.ics")
    assert len(service.deleted) == 1


FOREIGN_URIS = {
    "other host": "https://evil.example/caldav/tasks/ABC.ics",
    "host prefix": "https://dav.test.evil.example/caldav/tasks/ABC.ics",
    "userinfo trick": "https://dav.test@evil.example/caldav/tasks/ABC.ics",
    "other scheme": "http://dav.test/caldav/tasks/ABC.ics",
    "other port": "https://dav.test:8443/caldav/tasks/ABC.ics",
    "malformed port": "https://dav.test:x/caldav/tasks/ABC.ics",
    "relative": "/caldav/tasks/ABC.ics",
    "empty": "",
}


@pytest.mark.parametrize("uri", FOREIGN_URIS.values(), ids=list(FOREIGN_URIS))
def test_a_foreign_uri_never_reaches_the_client(service, uri):
    with pytest.raises(ToolError, match="Invalid task URI"):
        server_module.complete_task(uri)
    with pytest.raises(ToolError, match="Invalid task URI"):
        server_module.update_task(uri, title="x")
    assert service.deleted == [] and service.updated == []


def test_without_a_calendar_the_error_names_the_way_out(service):
    service.calendar_url = ""
    with pytest.raises(ToolError, match="set_calendar"):
        server_module.complete_task(TASK)


# ── calendars ────────────────────────────────────────────────────────────────

def test_set_calendar_accepts_a_listed_calendar(service):
    assert server_module.set_calendar("https://dav.test/caldav/work/") == "Calendar set: Work"
    assert service.calendar_url == "https://dav.test/caldav/work/"


def test_set_calendar_refuses_an_unlisted_url(service):
    with pytest.raises(ToolError, match="Unknown calendar"):
        server_module.set_calendar("https://evil.example/caldav/tasks/")
    assert service.calendar_url == CALENDAR


# ── tasks ────────────────────────────────────────────────────────────────────

def test_create_defaults_to_today(service, monkeypatch):
    monkeypatch.setattr(server_module, "_today", lambda: "2026-10-10")
    server_module.create_task("Call the dentist")
    assert service.created == [("Call the dentist", "2026-10-10", "")]


def test_create_keeps_an_explicit_date(service):
    server_module.create_task("Taxes", date="2026-12-01", note="Form 7")
    assert service.created == [("Taxes", "2026-12-01", "Form 7")]


def test_update_passes_only_what_was_given(service):
    server_module.update_task(TASK, date="2026-11-01")
    assert service.updated == [(TASK, "", "2026-11-01", None, None)]


def test_clear_note_wins_over_note(service):
    server_module.update_task(TASK, note="keep me", clear_note=True)
    assert service.updated == [(TASK, "", "", None, "")]


def test_catchup_moves_only_overdue_tasks(service, monkeypatch):
    monkeypatch.setattr(server_module, "_today", lambda: "2026-10-10")
    task = server_module.Task
    service.tasks = [
        task(id="1", uri=f"{CALENDAR}1.ics", title="Overdue", date="2026-10-01", starred=True),
        task(id="2", uri=f"{CALENDAR}2.ics", title="Today", date="2026-10-10"),
        task(id="3", uri=f"{CALENDAR}3.ics", title="Future", date="2026-11-01"),
        task(id="4", uri=f"{CALENDAR}4.ics", title="Undated"),
    ]
    assert server_module.catchup() == "1 overdue task(s) moved to today (2026-10-10)."
    assert service.updated == [(f"{CALENDAR}1.ics", "Overdue", "2026-10-10", True, None)]


def test_today_follows_the_configured_timezone(monkeypatch):
    """Just after midnight in Berlin it is still yesterday in UTC — which is
    what a container reports unless TZ says otherwise."""
    instant = datetime(2026, 10, 9, 22, 30, tzinfo=ZoneInfo("UTC"))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(server_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(server_module, "_TZ", ZoneInfo("Europe/Berlin"))
    assert server_module._today() == "2026-10-10"
    monkeypatch.setattr(server_module, "_TZ", ZoneInfo("UTC"))
    assert server_module._today() == "2026-10-09"
