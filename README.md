# MiniTask MCP

[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](https://www.python.org)
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed)](https://modelcontextprotocol.io)

An MCP server that lets an AI assistant manage your tasks on any **CalDAV**
server (Nextcloud, mailbox.org, Radicale, …) in natural language: list, create,
reschedule, star, annotate and complete them.

It is the MCP server of [MiniTask](https://github.com/Stefan-Schmidbauer/minitask),
the CalDAV desktop task manager, split out into its own repository so it runs
as one shared instance on a server, without the desktop app. It works on the
same tasks and follows the same model as the app.

## Concepts

- **Tasks live on a CalDAV server** as `VTODO` items. The server connects on the
  first tool call, with the account from its environment.
- **`uri`** is the stable handle for a task. Always obtain it from `list_tasks`
  before updating or completing a task.
- **Stars** map to the iCalendar `PRIORITY:1` property.
- **Completing a task deletes it** from the server — exactly like checking it
  off in the desktop app. There is no separate "done" list.
- **Dates** are plain `YYYY-MM-DD` strings (the `DUE` date), with no time
  component. What "today" is follows `TZ`.
- **Notes** map to the iCalendar `DESCRIPTION` property.

Coming from the `mcp_server.py` that used to ship inside MiniTask: the tools and
their arguments are unchanged, but nothing is shared with the app any more —
the account is configured here, and `set_calendar` lasts only until the server
restarts (`CALDAV_CALENDAR_URL` is the permanent setting).

## Tools

| Tool | Description |
|---|---|
| `list_tasks` | All open tasks in the current calendar: `id`, `uri`, `title`, `date`, `starred`, `completed`, `note` |
| `create_task` | Create a task — `title`, optional `date` (**defaults to today**) and `note` |
| `update_task` | Edit a task by `uri` — pass only what should change (`title`, `date`, `starred`, `note`); the rest is kept. `clear_note: true` removes the note |
| `complete_task` | Mark a task as done by `uri` — removes it from the server |
| `catchup` | Move every overdue task to today — same as "Catch Up" in the app |
| `list_calendars` | List the calendars of the account as `{name, url}` |
| `set_calendar` | Switch to another calendar from `list_calendars` — **until the server restarts**; `CALDAV_CALENDAR_URL` is the permanent setting |

To remove a note, use `clear_note` rather than an empty `note`: some MCP clients
drop empty-string arguments before they reach the server.

Typical prompts and what they map to:

- *"What's on my list?"* → `list_tasks`
- *"Add a starred task to call the dentist tomorrow"* → `create_task`, then `update_task`
- *"Push the tax return to next Friday"* → `list_tasks` → `update_task` (date only)
- *"I finished the grocery shopping"* → `list_tasks` → `complete_task`
- *"Catch me up — move everything overdue to today"* → `catchup`

## Configuration

Everything is configured through environment variables. Account settings and
secrets go into the env file on the server,
[`deploy/env.example`](deploy/env.example) is the template; the deployment
wiring is set in [`deploy/docker-compose.snippet.yml`](deploy/docker-compose.snippet.yml).

| Variable | |
|---|---|
| `CALDAV_URL` | **Required.** The CalDAV endpoint, e.g. `https://cloud.example.net/remote.php/dav` |
| `CALDAV_USERNAME` | **Required.** |
| `CALDAV_PASSWORD` | **Required.** Use an app password where the server offers one |
| `CALDAV_CALENDAR_URL` | The calendar to work on — a `url` from `list_calendars`. Without it no calendar is selected until `set_calendar` is called |
| `TZ` | IANA name (e.g. `Europe/Berlin`) deciding what "today" is. Set in the snippet — without it the container runs in UTC and dates new tasks to yesterday just after local midnight |
| `MCP_AUTH_TOKEN` | **Required.** Clients send `Authorization: Bearer <token>`. Without it the server refuses to start. Generate with `openssl rand -hex 32` |
| `MCP_PATH` | The path the endpoint is served under — `/minitask-mcp` in the snippet |
| `MCP_ALLOWED_HOSTS` | Comma-separated. The hostname the reverse proxy answers on, or every request is answered `421 Invalid Host header`. Setting it *replaces* the localhost defaults, so the snippet lists `127.0.0.1:9152` as well |
| `MCP_ALLOWED_ORIGINS` | Comma-separated. A client that sends an `Origin` header is answered `403` unless it is listed |
| `MCP_ALLOW_UNAUTHENTICATED` | `1` starts without a token — only for a deployment that authenticates further out |
| `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` | Set by the snippet (`streamable-http`, `0.0.0.0`, `8000`); no need to change them |

## Deployment

The server runs as a container on a Docker host and is reached over HTTPS.
`deploy.sh` builds the image *on that host*, through a Docker context over SSH —
no registry, no source checkout on the server — and recreates the container
through the compose file that already lives there.

**Prerequisites**

- A server with Docker Compose and a compose stack, reachable by SSH key as a
  user in the `docker` group, with `curl` installed.
- Docker with the `buildx` plugin on your machine (`docker buildx version`).
  The build runs on the server, but the builder is chosen locally, and the
  `Dockerfile`'s `COPY --chmod` needs BuildKit.
- A reverse proxy with TLS on the server — Tailscale, Caddy, nginx with
  Let's Encrypt, whatever is already there.

### 1. Clone

```bash
git clone https://github.com/Stefan-Schmidbauer/minitask-mcp.git
cd minitask-mcp
```

### 2. Prepare the server (once)

- Paste [`deploy/docker-compose.snippet.yml`](deploy/docker-compose.snippet.yml)
  under `services:` in the compose file on the server and adjust what its
  header names. The port is published on localhost only (`127.0.0.1:9152`).
- Create `.env.minitask-mcp` next to the compose file from
  [`deploy/env.example`](deploy/env.example), mode `640`, and fill it in.
- `CALDAV_CALENDAR_URL` may stay empty for the first start: ask the assistant
  to list your calendars, then set it and run `./deploy.sh --no-build`.

`deploy.sh` never creates the compose file and never copies secrets: both stay
on the server and belong to its admin, who can operate the service (`up -d`,
`logs`) without this repository. The cost is that `deploy/` holds *copies* that
nothing keeps in sync.

### 3. Deploy

```bash
cp deploy/deploy.env.example deploy.env    # set DEPLOY_HOST
./deploy.sh
```

It builds the image on the server, recreates this one service — with
`--no-deps`, so nothing else in the stack is touched — and waits for `/healthz`.

| `deploy.env` | Default |
|---|---|
| `DEPLOY_HOST` | none — **required**, e.g. `admin@your-server.example.net`. Passed to `ssh` verbatim, so it must be the name your `~/.ssh/config` matches |
| `DOCKER_CTX` | the host from `DEPLOY_HOST`, e.g. `your-server.example.net` — the Docker context, created on the first run and shared by every service on that host. `deploy.sh` stops if an existing context of that name points elsewhere |
| `REMOTE_COMPOSE` | `/opt/docker/docker-compose.yml` |
| `HEALTH_URL` | `http://127.0.0.1:9152/healthz` — resolved on the server |

`deploy.env` is gitignored; exported variables of the same name take precedence.

### 4. Put TLS in front

The endpoint has no encryption of its own. Point the reverse proxy at
`http://127.0.0.1:9152/minitask-mcp`, keep the path unchanged, and put the hostname
it answers on into `MCP_ALLOWED_HOSTS` (and `https://<hostname>` into
`MCP_ALLOWED_ORIGINS`). With Tailscale, for example, inside the tailnet only:

```bash
tailscale serve --bg --set-path /minitask-mcp http://127.0.0.1:9152/minitask-mcp
```

Check it from your machine:

```bash
curl -s https://mcp.example.net/minitask-mcp -H 'Authorization: Bearer <MCP_AUTH_TOKEN>'
```

`Bad Request: Missing session ID` is the right answer: proxy, path, token and
host check are all in order, and the server is asking for an MCP session.
`Invalid Host header` means the hostname is missing from `MCP_ALLOWED_HOSTS`,
`unauthorized` that the token is wrong.

### 5. Connect a client

The endpoint is `https://mcp.example.net/minitask-mcp`, with the header
`Authorization: Bearer <MCP_AUTH_TOKEN>`. [`.mcp.json.example`](.mcp.json.example)
shows it for Claude Code; any MCP client that speaks streamable HTTP works the
same way.

### Updates and rollback

```bash
./deploy.sh              # after a code change: build, recreate, health check
./deploy.sh --no-build   # after a change to the env file: recreate only
ssh admin@your-server.example.net docker logs --tail 50 minitask-mcp
```

A changed env file takes effect only when the container is *recreated*;
`docker compose restart` keeps the old environment.

Each build is tagged twice: `:local`, which the compose file references, and a
version tag from `git describe` — `v1.2.0` on a release tag, `v1.2.0-3-gafe0bf9`
three commits past it, `-dirty` for uncommitted changes — that stays behind for
a rollback. It is also the version the server reports to clients
(`serverInfo.version`), so a client can tell which build answers. To roll back:

```bash
docker --context your-server.example.net images minitask-mcp      # what is on the server

ssh admin@your-server.example.net \
  'docker tag minitask-mcp:v1.2.0 minitask-mcp:local && \
   docker compose -f /opt/docker/docker-compose.yml up -d --no-deps minitask-mcp'
```

The image exists only in the server's local store, so the service definition
must keep `pull_policy: never` — without it, a routine `docker compose pull`
fails on this service and aborts the pull for the whole stack.

### Troubleshooting

| What you see | Cause and fix |
|---|---|
| The container restarts in a loop | It exits at startup — `docker logs minitask-mcp` has the reason, usually a missing value in the env file |
| A tool answers `Cannot connect to the CalDAV server: …` | URL, username or password are wrong, or the CalDAV server is not reachable from the container. The rest of the message is the underlying error |
| It exits with `ZoneInfoNotFoundError` | `TZ` is not an IANA name such as `Europe/Berlin` |
| A tool answers `No calendar selected` | Set `CALDAV_CALENDAR_URL` and recreate the container; `set_calendar` only helps until the next restart |
| `Invalid task URI` or `Unknown calendar` | The value was not taken from `list_tasks` / `list_calendars`. The server accepts nothing else — see [Security model](#security-model) |
| `Task not found` | The task was completed or deleted in the meantime — call `list_tasks` again |
| New tasks get yesterday's date shortly after midnight | `TZ` is not set |
| It exits with `MCP_AUTH_TOKEN is not set` | The server refuses to run without a token. Set one in the env file |
| `401 unauthorized` | Token missing or wrong. The header is exactly `Authorization: Bearer <token>` |
| `421 Invalid Host header` | The hostname the request arrives under is not in `MCP_ALLOWED_HOSTS` |
| `403 Invalid Origin header` | The client sends an `Origin` that is not in `MCP_ALLOWED_ORIGINS` |
| `404 Not Found` with a valid token | The URL path is not `MCP_PATH`, or the proxy rewrites it |
| A bare `Error executing tool <name>` | An unexpected failure; the traceback is in `docker logs minitask-mcp` |
| *healthy*, but every tool call fails | `/healthz` says the process is alive, nothing more — the server only connects to the CalDAV server on the first tool call |

## Security model

- The server holds the CalDAV credentials and acts with them. Whoever can call
  its tools can read, change and delete the tasks of that account.
- `MCP_AUTH_TOKEN` is a static shared secret — no rotation, no per-client
  identity, no rate limiting. It is a second layer behind a network boundary,
  not a replacement for one: run the endpoint inside an internal network or a
  VPN, not on the public internet.
- Task URIs and calendar URLs are tool arguments and therefore come from the
  model. The server only accepts a task URI on the current calendar's server,
  and only a calendar the account actually lists — so the credentials are never
  sent to a host named in a tool call.
- Task titles and notes reach the model as text it reads, so a task can carry
  instructions aimed at the model. The mitigation is the MCP client's
  tool-approval prompts; the reading tools are marked read-only and
  `complete_task` destructive so a client can tell them apart.

Vulnerabilities: see [SECURITY.md](SECURITY.md).

## Development

```bash
python3 -m venv venv
venv/bin/pip install --require-hashes -r requirements.txt
venv/bin/pip install -r requirements-dev.txt
venv/bin/ruff check .
venv/bin/pytest -q
```

| Test | What it pins |
|---|---|
| `tests/test_tools.py` | The tool layer against a fake service: which calendar and task URI are accepted, what "today" means. No CalDAV server is contacted |
| `tests/test_auth.py` | The HTTP front door: bearer check, health endpoint, startup guard |

Without `MCP_TRANSPORT` the server speaks stdio, which is the quickest way to
try a change before deploying it — with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector), passing the
variables with `-e`:

```bash
npx @modelcontextprotocol/inspector --cli venv/bin/python server.py \
  --method tools/list \
  -e CALDAV_URL=… -e CALDAV_USERNAME=… -e CALDAV_PASSWORD=…
```

### Dependencies

`requirements.in` holds the direct dependencies; `requirements.txt` is generated
from it with hashes and is what the image installs (`--require-hashes`). To bump
a version, edit `requirements.in`, regenerate, and commit both together:

```bash
venv/bin/pip-compile --generate-hashes --strip-extras requirements.in
```

CI runs lint and tests, fails if `requirements.txt` no longer matches
`requirements.in`, and runs `pip-audit` against the pinned set. Dependabot
proposes the bumps monthly.

## License

MIT — see [LICENSE](LICENSE).

**Stefan Schmidbauer** — [GitHub](https://github.com/Stefan-Schmidbauer).
Built with [Claude Code](https://claude.com/claude-code) as co-author.
