# LLM BAN

A self-hosted kanban for human-reviewed autonomous coding work. Includes a PHP
board, SQLite storage, a Python work registry, planner/doer/tester workers, and
optional outbound Telegram notifications. No cloud account or Telegram is
required to use the board.

**Initial public release:** single trusted team, one serialized worker, explicit
human approval between planning and implementation. It is not a multi-tenant
service or a security sandbox for untrusted contributors.

## Quick start (Docker Compose)

Requires Git, Python 3.11+, Docker Engine and Docker Compose v2+. Linux and Docker
Desktop are supported. Runtime containers use unprivileged users; only the
one-shot volume initializer runs as root and has no network or host mounts.

```sh
git clone https://github.com/Anatolt/llm-ban-pub.git
cd llm-ban-pub
python3 scripts/configure.py
docker compose up -d --build web
```

Open **http://localhost:8080** on the machine running Docker. Sign in with the
username and randomly generated password in your private `.env`. The board
starts empty. Create a card with the **+** in `man backlog`.

Remote server: use an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 your-server`) or put
a TLS reverse proxy in front. Basic authentication must travel over HTTPS outside
localhost. Set `LLMBAN_COOKIE_SECURE=1` for HTTPS. The default port binds only to
loopback. See [installation](docs/INSTALL.md) for deployment and native setup.

## Enable autonomous agents

1. Clone the repositories you want agents to work on into `workspaces/` (or set
   `LLMBAN_WORKSPACES` to a dedicated host directory). Never mount your entire
   home, SSH credentials, Docker socket, or production directories.
2. Edit `config/agents.json`: map each project slug to its **container path**,
   e.g. `"my-app": "/workspaces/my-app"`. The card's project selects this map;
   a card cannot supply a working directory or agent command.
3. Set `LLMBAN_PI_PROVIDER`, `LLMBAN_PI_MODEL` and that provider's API key in
   `.env`. The image includes pinned [Pi](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)
   `1.4.0`. Use any supported provider/model with tool calling. Select these
   yourself; there is no default paid provider or automatic model fallback.
4. On Linux, set `LLMBAN_UID`/`LLMBAN_GID` to the owner of the mounted repositories
   (the setup script uses your non-root UID/GID). Do not use UID 0.
5. Start the worker:

```sh
docker compose --profile agents up -d --build
docker compose logs -f worker
```

The default adapter runs Pi without discovered extensions, skills or saved
sessions. Project `AGENTS.md` context still applies. To use another agent, replace
each role's argv array in `config/agents.json`; see the
[agent contract](docs/AGENTS.md). The worker itself has no Python dependencies
outside the standard library.

| Put card in | Agent works in | Successful result |
| --- | --- | --- |
| `llm plan` | `llm planing` | `man plan` — review the plan |
| `llm todo` | `llm doing` | `ready-to-test` — tester picks it up |
| `ready-to-test` or `llm test` | `llm testing` | `man test` — accept manually |

A human moves the reviewed plan to `llm todo` and the accepted result to
`man done`. Drop a card between two others to set its order; the worker picks
the topmost card and appends the cards it moves to the bottom of the
destination column. Failures go to `man plan` (planner failures to `man backlog`) with a
report. There are no automatic retries of implementation. Timeouts stop the
process group. An interrupted run blocks new work until explicit recovery.
Generated changes remain in the workspace for review; agents are instructed
not to commit, push or deploy. Inspect the diff yourself before accepting work.

## Optional Telegram

Set these in `.env`:

```dotenv
LLMBAN_TELEGRAM_ENABLED=1
LLMBAN_TELEGRAM_TOKEN=
LLMBAN_TELEGRAM_CHAT_ID=
LLMBAN_PUBLIC_URL=
```

Fill the token/chat ID locally. The public URL is optional; when set it must be
HTTPS without credentials. Then:

```sh
docker compose --profile agents --profile telegram up -d --build
```

Only card ID, role and result status are sent. Task descriptions, transcripts and
credentials are not forwarded. Delivery failures stay in a persistent outbox and
retry with backoff; a crash after sending can cause a duplicate notification.
There are no inbound Telegram commands. To disable, set the flag to `0`, recreate
the worker and stop `telegram` (`docker compose stop telegram`).

## CLI and work registry

```sh
docker compose run --rm init python3 bin/board.py create \
  --title 'Implement a small feature' --project my-app --column 'man backlog'
docker compose run --rm init python3 bin/board.py runs
docker compose run --rm init python3 bin/memctl.py --json-min boot
```

`Current Work` shows worker runs and manually registered work. The registry is
advisory: overlap warnings do not enforce filesystem isolation. Its complete CLI
is available with `python3 bin/memctl.py --help`; set `MEMCTL_DB`/`LLMBAN_DB` and
`MEMCTL_SNAPSHOT_ROOT` when integrating your own agents.

## Development and operations

```sh
python3 -m unittest discover -s tests -v
php -l public/index.php
```

Tests exercise real PHP HTTP requests and the complete worker pipeline using an
offline fixture agent. They do not spend API credits. See
[operations](docs/OPERATIONS.md), [security](SECURITY.md), and
[contribution guide](CONTRIBUTING.md). Licensed under [MIT](LICENSE).
