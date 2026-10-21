"""MiniTask MCP Server — CalDAV task management (VTODO) as MCP tools."""

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hmac import compare_digest
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import caldav
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


def _env_flag(name: str) -> bool:
    """An on/off switch from the environment.

    Not `bool(os.getenv(...))`: a variable set to "0" or "false" reads as *off*
    the way anyone writing it into a compose file expects, and anything the list
    does not name falls to the safe side rather than counting as on because it is
    non-empty.
    """
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


CALDAV_URL = os.getenv("CALDAV_URL", "")
CALDAV_USERNAME = os.getenv("CALDAV_USERNAME", "")
CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD", "")
# The calendar the task tools work on. Optional: without it, nothing is selected
# until set_calendar picks one, and that choice lasts only as long as the process.
CALDAV_CALENDAR_URL = os.getenv("CALDAV_CALENDAR_URL", "")
# IANA name (e.g. Europe/Berlin) that decides what "today" is — read from the
# standard TZ variable, which sets the container's local time as well. A
# container runs in UTC unless told otherwise, which would date new tasks — and
# move overdue ones — to yesterday for the first hours after local midnight.
TIMEZONE = os.getenv("TZ", "").removeprefix(":")

# Transport: "stdio" (default, the client spawns the server) or
# "streamable-http" (one shared instance, which is how the container runs it).
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")
# Serving HTTP without MCP_AUTH_TOKEN is refused rather than warned about, because
# reaching the endpoint means reaching the CalDAV account. This is the deliberate
# opt-out for a deployment that authenticates one layer further out.
MCP_ALLOW_UNAUTHENTICATED = _env_flag("MCP_ALLOW_UNAUTHENTICATED")
# Hosts/origins accepted by the SDK's DNS-rebinding protection. Comma-separated;
# needed because we are reached through a reverse proxy under its own hostname.
MCP_ALLOWED_HOSTS = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
MCP_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]

if not (CALDAV_URL and CALDAV_USERNAME and CALDAV_PASSWORD):
    raise OSError(
        "CALDAV_URL, CALDAV_USERNAME and CALDAV_PASSWORD must all be set.\n"
        "Put them in the env file next to the compose file (deploy/env.example is\n"
        "the template) — see the Configuration section of the README."
    )

logger = logging.getLogger("minitask-mcp")

mcp = MCPServer("minitask", version=os.environ.get("MCP_SERVER_VERSION", ""))

# Raises ZoneInfoNotFoundError on a misspelt name — at startup, not on the first
# create_task after midnight.
_TZ = ZoneInfo(TIMEZONE) if TIMEZONE else None

# Failures the model can act on are raised as ToolError: only its text reaches
# the model. Any other exception is a crash to the SDK — the model sees a bare
# "Error executing tool <name>" and the traceback goes to the server log.
_NO_CALENDAR = (
    "No calendar selected — set CALDAV_CALENDAR_URL, or call list_calendars and then set_calendar."
)


def _today() -> str:
    return datetime.now(_TZ).date().isoformat()


# ── CalDAV ───────────────────────────────────────────────────────────────────

@dataclass
class Task:
    id: str
    uri: str
    title: str
    date: str = ""
    completed: bool = False
    starred: bool = False
    note: str = ""


class CalDAVService:
    def __init__(self):
        self._client: caldav.DAVClient | None = None
        self._principal: caldav.Principal | None = None
        self._calendar: caldav.Calendar | None = None

    def connect(self, url: str, username: str, password: str) -> bool:
        try:
            # A timeout, because this is a long-running server now: without one a
            # CalDAV server that stops answering holds a worker thread per call.
            self._client = caldav.DAVClient(url=url, username=username, password=password, timeout=30)
            self._principal = self._client.principal()
            logger.info("Connected to CalDAV server: %s", url)
            return True
        except Exception as e:
            logger.error("Connection failed: %s", e)
            raise ConnectionError(str(e)) from e

    @property
    def calendar_url(self) -> str:
        return str(self._calendar.url) if self._calendar else ""

    def get_calendars(self) -> list[dict]:
        if not self._principal:
            return []
        calendars = []
        for cal in self._principal.calendars():
            name = cal.get_display_name() or str(cal.url)
            calendars.append({
                "url": str(cal.url),
                "name": name,
            })
        return calendars

    def set_current_calendar(self, calendar_url: str) -> None:
        if not self._client:
            raise ConnectionError("Not connected")
        self._calendar = caldav.Calendar(client=self._client, url=calendar_url)
        logger.info("Calendar set: %s", calendar_url)

    def get_tasks(self) -> list[Task]:
        if not self._calendar:
            raise ToolError(_NO_CALENDAR)
        tasks = []
        try:
            todos = self._calendar.todos(include_completed=False)
        except Exception as e:
            logger.error("Failed to fetch tasks: %s", e)
            raise
        for todo_obj in todos:
            try:
                task = self._parse_todo(todo_obj)
                if task:
                    tasks.append(task)
            except Exception as e:
                logger.warning("Failed to parse todo: %s", e)
        return tasks

    def create_task(self, title: str, date_str: str = "", note: str = "") -> bool:
        if not self._calendar:
            raise ToolError(_NO_CALENDAR)
        uid = str(uuid.uuid4()).upper()
        now = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//MiniTask//EN",
            "BEGIN:VTODO",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"CREATED:{now}",
            f"SUMMARY:{self._escape_ical(title)}",
            "STATUS:NEEDS-ACTION",
        ]
        if date_str:
            lines.append(f"DUE;VALUE=DATE:{date_str.replace('-', '')}")
        if note:
            lines.append(f"DESCRIPTION:{self._escape_ical(note)}")
        lines += ["END:VTODO", "END:VCALENDAR"]
        try:
            self._calendar.save_todo("\r\n".join(lines) + "\r\n")
            logger.info("Task created: %s", title)
            return True
        except Exception as e:
            logger.error("Failed to create task: %s", e)
            raise

    def update_task(self, uri: str, title: str = "", date_str: str = "",
                    starred: bool | None = None, note: str | None = None) -> bool:
        if not self._calendar:
            raise ToolError(_NO_CALENDAR)
        try:
            todo_obj = caldav.Todo(client=self._client, url=uri, parent=self._calendar)
            todo_obj.load()

            uid = ""
            existing_summary = ""
            existing_due = ""
            existing_priority = ""
            for line in todo_obj.data.splitlines():
                if line.startswith("UID:"):
                    uid = line[4:].strip()
                elif line.startswith("SUMMARY"):
                    existing_summary = line
                elif line.upper().startswith("DUE"):
                    existing_due = line
                elif line.startswith("PRIORITY:"):
                    existing_priority = line

            # DESCRIPTION can be folded across multiple physical lines, so read it
            # via the icalendar parser (which unfolds/unescapes) instead of raw lines.
            existing_note = ""
            try:
                for component in todo_obj.icalendar_instance.walk("VTODO"):
                    desc = component.get("description")
                    if desc:
                        existing_note = str(desc)
                    break
            except Exception as e:
                logger.warning("Failed to read existing description: %s", e)

            now = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
            lines = [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//MiniTask//EN",
                "BEGIN:VTODO",
                f"UID:{uid}",
                f"DTSTAMP:{now}",
            ]

            if title:
                lines.append(f"SUMMARY:{self._escape_ical(title)}")
            elif existing_summary:
                lines.append(existing_summary)

            if date_str:
                lines.append(f"DUE;VALUE=DATE:{date_str.replace('-', '')}")
            elif existing_due:
                lines.append(existing_due)

            if starred is True:
                lines.append("PRIORITY:1")
            elif starred is None and existing_priority:
                lines.append(existing_priority)
            # starred is False → no PRIORITY line = star removed

            # note is None → keep existing; "" → clear; text → set
            if note is not None:
                if note:
                    lines.append(f"DESCRIPTION:{self._escape_ical(note)}")
            elif existing_note:
                lines.append(f"DESCRIPTION:{self._escape_ical(existing_note)}")

            lines.append("STATUS:NEEDS-ACTION")

            lines += ["END:VTODO", "END:VCALENDAR"]

            todo_obj.data = "\r\n".join(lines) + "\r\n"
            todo_obj.save()
            logger.info("Task updated: %s", uri)
            return True
        except Exception as e:
            logger.error("Failed to update task: %s", e)
            raise

    @staticmethod
    def _escape_ical(text: str) -> str:
        if not text:
            return ""
        return text.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    def delete_task(self, uri: str) -> bool:
        if not self._calendar:
            raise ToolError(_NO_CALENDAR)
        try:
            self._client.delete(uri)
            logger.info("Task deleted: %s", uri)
            return True
        except Exception as e:
            logger.error("Failed to delete task: %s", e)
            raise

    def _parse_todo(self, todo_obj) -> Task | None:
        try:
            ical = todo_obj.icalendar_instance
            for component in ical.walk("VTODO"):
                uid = str(component.get("uid", ""))
                summary = str(component.get("summary", "Untitled"))
                due = component.get("due")
                date_str = ""
                if due:
                    due_val = due.dt
                    if isinstance(due_val, datetime):
                        date_str = due_val.strftime("%Y-%m-%d")
                    elif isinstance(due_val, date):
                        date_str = due_val.isoformat()
                priority = component.get("priority")
                starred = int(str(priority)) == 1 if priority else False
                completed = component.get("completed") is not None
                description = component.get("description")
                note = str(description) if description else ""
                return Task(
                    id=uid,
                    uri=str(todo_obj.url),
                    title=summary,
                    date=date_str,
                    completed=completed,
                    starred=starred,
                    note=note,
                )
        except Exception as e:
            logger.warning("Parse error: %s", e)
        return None


_service = CalDAVService()
_connected = False
# The SDK runs sync tools in a threadpool under the HTTP transport, so two first
# calls can arrive together; without the lock both would connect.
_connect_lock = threading.Lock()


def _ensure_connected() -> None:
    global _connected
    with _connect_lock:
        if _connected:
            return
        try:
            _service.connect(CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD)
        except ConnectionError as e:
            raise ToolError(f"Cannot connect to the CalDAV server: {e}") from e
        if CALDAV_CALENDAR_URL:
            _service.set_current_calendar(CALDAV_CALENDAR_URL)
        _connected = True


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(parts.scheme)


def _task_uri(uri: str) -> str:
    """Refuse a task URI that does not point at the current calendar's server.

    Tool arguments come from the model, and the CalDAV client attaches the
    account's credentials to whatever URL it is handed — `delete_task` sends the
    request without checking the host. A note is free text the model reads, so
    without this a task could talk it into sending those credentials anywhere.
    URIs from list_tasks are absolute and on the calendar's own server; anything
    else is not one of ours.
    """
    calendar_url = _service.calendar_url
    if not calendar_url:
        raise ToolError(_NO_CALENDAR)
    try:
        same_server = _origin(uri) == _origin(calendar_url)
    except ValueError:  # urlsplit: malformed port
        same_server = False
    if not same_server:
        raise ToolError(f"Invalid task URI {uri!r} — pass a uri exactly as list_tasks returns it.")
    return uri


# ── Tools ────────────────────────────────────────────────────────────────────

_READ_ONLY = ToolAnnotations(read_only_hint=True)


@mcp.tool(annotations=_READ_ONLY)
def list_calendars() -> list[dict]:
    """List all available calendars on the CalDAV server."""
    _ensure_connected()
    return _service.get_calendars()


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False))
def set_calendar(calendar_url: str) -> str:
    """Switch to a different calendar by URL. Use list_calendars to find the URL.

    The switch lasts until the server restarts; it then returns to the calendar
    it is configured with (CALDAV_CALENDAR_URL).
    """
    _ensure_connected()
    # Membership rather than "any URL": the client would send the account's
    # credentials to whatever it is pointed at.
    match = next((c for c in _service.get_calendars() if c["url"] == calendar_url), None)
    if match is None:
        raise ToolError(
            f"Unknown calendar {calendar_url!r} — pass a url exactly as list_calendars returns it."
        )
    _service.set_current_calendar(calendar_url)
    return f"Calendar set: {match['name']}"


@mcp.tool(annotations=_READ_ONLY)
def list_tasks() -> list[dict]:
    """List all open (incomplete) tasks in the current calendar."""
    _ensure_connected()
    tasks = _service.get_tasks()
    return [
        {
            "id": t.id,
            "uri": t.uri,
            "title": t.title,
            "date": t.date,
            "starred": t.starred,
            "completed": t.completed,
            "note": t.note,
        }
        for t in tasks
    ]


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False))
def create_task(title: str, date: str = "", note: str = "") -> str:
    """Create a new task. Date format: YYYY-MM-DD (e.g. 2026-05-30). Defaults to today if omitted.

    Args:
        title: The task summary
        date: Due date (YYYY-MM-DD), defaults to today if omitted
        note: Optional free-text note (the VTODO DESCRIPTION), empty = no note
    """
    _ensure_connected()
    _service.create_task(title, date or _today(), note)
    return f"Task created: {title}"


@mcp.tool()
def update_task(
    uri: str,
    title: str = "",
    date: str = "",
    starred: bool | None = None,
    note: str | None = None,
    clear_note: bool = False,
) -> str:
    """Update an existing task. The URI comes from list_tasks.

    Only pass the fields you want to change; omitted fields keep their current value.

    Args:
        uri: Task URI (from list_tasks)
        title: New task title, empty = keep existing title
        date: Due date (YYYY-MM-DD), empty = keep existing date
        starred: True = add star, False = remove star, omit = keep unchanged
        note: New note text, omit = keep unchanged. To remove the note, set
            clear_note=True instead of passing an empty string.
        clear_note: True = delete the note entirely. Takes precedence over note.
    """
    _ensure_connected()
    effective_note = "" if clear_note else note
    try:
        _service.update_task(_task_uri(uri), title, date, starred, effective_note)
    except caldav.error.NotFoundError as e:
        raise ToolError(
            "Task not found — it may have been completed already. Call list_tasks for the current URIs."
        ) from e
    return f"Task updated: {title}" if title else "Task updated."


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def complete_task(uri: str) -> str:
    """Mark a task as done — removes it from the server, same as checking it off in the app.

    Args:
        uri: Task URI (from list_tasks)
    """
    _ensure_connected()
    _service.delete_task(_task_uri(uri))
    return "Task completed."


@mcp.tool()
def catchup() -> str:
    """Set all overdue tasks to today. Same as 'Catch Up' in the desktop app."""
    _ensure_connected()
    today = _today()
    tasks = _service.get_tasks()
    overdue = [t for t in tasks if t.date and t.date < today]
    for task in overdue:
        _service.update_task(task.uri, task.title, today, task.starred)
    return f"{len(overdue)} overdue task(s) moved to today ({today})."


# ── HTTP transport ───────────────────────────────────────────────────────────

class HealthzMiddleware:
    """Answer /healthz, always and without authentication.

    Its own layer *outside* the bearer check: a health probe carries no token,
    and deploy.sh and the image's HEALTHCHECK both poll it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            await _plain_response(send, 200, b"ok")
            return
        await self.app(scope, receive, send)


class BearerAuthMiddleware:
    """Pure-ASGI bearer check.

    Deliberately not a BaseHTTPMiddleware subclass: that one buffers the
    response and would break the streaming/SSE responses of the MCP transport.
    """

    def __init__(self, app, token: str):
        self.app = app
        # Compared as bytes, exactly as the header arrives: decoding first means
        # a non-UTF-8 header raises UnicodeDecodeError, and compare_digest on str
        # rejects non-ASCII with a TypeError — either turns a request anyone can
        # send into a 500 with a traceback instead of a clean 401.
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        if not compare_digest(provided, self.expected):
            logger.warning("Rejected unauthenticated request to %s", scope.get("path"))
            await _plain_response(send, 401, b"unauthorized")
            return
        await self.app(scope, receive, send)


async def _plain_response(send, status: int, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _bearer_check_or_refuse(token: str, allow_unauthenticated: bool) -> bool:
    """Decide whether an HTTP run may start, and whether it is guarded.

    A refusal rather than a warning: in HTTP mode reaching the endpoint means
    reaching the CalDAV account, so an unauthenticated endpoint is a decision,
    not a default one can slip into by leaving a variable unset.

    Returns True when the bearer check will be installed, False when the
    operator opted out explicitly. Raises OSError when neither is the case.
    """
    if token:
        return True
    if not allow_unauthenticated:
        raise OSError(
            "MCP_AUTH_TOKEN is not set, so the HTTP endpoint would be unauthenticated —\n"
            "and reaching it means reading, changing and deleting the tasks of the\n"
            "configured CalDAV account; this is the only check in front of it.\n"
            "Set MCP_AUTH_TOKEN to a random secret (openssl rand -hex 32), or set\n"
            "MCP_ALLOW_UNAUTHENTICATED=1 if something in front of the server does the\n"
            "authentication and you accept that. Neither replaces a network boundary."
        )
    logger.warning(
        "MCP_ALLOW_UNAUTHENTICATED is set — the endpoint is served without a bearer "
        "check. Anyone who can reach it has full access to the task list."
    )
    return False


def _run_http() -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    # Before anything binds a port: an unauthenticated endpoint must not come up
    # far enough to answer a single request.
    guarded = _bearer_check_or_refuse(MCP_AUTH_TOKEN, MCP_ALLOW_UNAUTHENTICATED)
    # Leaving transport_security None keeps the SDK's localhost-only default,
    # which is what makes a proxied request fail closed with 421 until its
    # hostname is listed — see MCP_ALLOWED_HOSTS in the README.
    transport_security = None
    if MCP_ALLOWED_HOSTS or MCP_ALLOWED_ORIGINS:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=MCP_ALLOWED_HOSTS,
            allowed_origins=MCP_ALLOWED_ORIGINS,
        )
        logger.info("DNS-rebinding protection allows hosts: %s", MCP_ALLOWED_HOSTS)
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=transport_security,
    )
    if guarded:
        app = BearerAuthMiddleware(app, MCP_AUTH_TOKEN)
    # Outermost, so the probe answers with or without a bearer token.
    app = HealthzMiddleware(app)
    logger.info("Serving MCP over HTTP on %s:%s%s", MCP_HOST, MCP_PORT, MCP_PATH)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    if MCP_TRANSPORT == "streamable-http":
        logging.basicConfig(level=logging.INFO)
        _run_http()
    else:
        mcp.run()
