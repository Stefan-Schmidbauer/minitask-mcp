"""Shared fixtures.

`server.py` reads every setting from the environment at module scope and raises
`OSError` without credentials, so the environment has to be set up *before* the
import — not inside a fixture. That is why the assignments below sit at module
level and the import follows them.
"""
import os

os.environ.setdefault("CALDAV_URL", "https://dav.test/caldav/")
os.environ.setdefault("CALDAV_USERNAME", "tester")
os.environ.setdefault("CALDAV_PASSWORD", "test-password")

import pytest

import server as _server

CALENDAR = "https://dav.test/caldav/tasks/"


class FakeService:
    """Stands in for CalDAVService and records what the tools ask of it.

    The tools are a thin layer over the service, so what is worth pinning is
    what they pass down — and, for the URI check, that they pass nothing at all.
    """

    def __init__(self):
        self.calendar_url = CALENDAR
        self.calendars = [
            {"url": CALENDAR, "name": "Tasks"},
            {"url": "https://dav.test/caldav/work/", "name": "Work"},
        ]
        self.tasks: list[_server.Task] = []
        self.created: list[tuple] = []
        self.updated: list[tuple] = []
        self.deleted: list[str] = []

    def get_calendars(self):
        return self.calendars

    def set_current_calendar(self, calendar_url):
        self.calendar_url = calendar_url

    def get_tasks(self):
        return self.tasks

    def create_task(self, title, date_str="", note=""):
        self.created.append((title, date_str, note))

    def update_task(self, uri, title="", date_str="", starred=None, note=None):
        self.updated.append((uri, title, date_str, starred, note))

    def delete_task(self, uri):
        self.deleted.append(uri)


@pytest.fixture
def service(monkeypatch):
    """The server module wired to a FakeService, already 'connected'."""
    fake = FakeService()
    monkeypatch.setattr(_server, "_service", fake)
    monkeypatch.setattr(_server, "_connected", True)
    return fake
