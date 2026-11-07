# Operations

## Stop and recover

```sh
docker compose stop worker
docker compose run --rm --no-deps worker python3 bin/board.py runs
```

Graceful stop terminates the active process group and routes the card for human
review. If the host/container was killed abruptly, a `running` record blocks
new work. Confirm all old agent processes have stopped, inspect the workspace,
then explicitly recover:

```sh
docker compose run --rm --no-deps worker python3 bin/worker.py --recover
docker compose --profile agents up -d worker
```

Recovery marks unfinished runs failed; it does not roll back files, retry a task,
or infer completion. Retry by manually reviewing and requeuing the card.
Timeouts, invalid JSON, missing workspaces and nonzero agent exits also return
to human review. Inspect reports in card descriptions or `bin/board.py runs`.

## Backup

Back up the SQLite database using its online backup API, not a plain copy of the
main file while WAL writes may be in progress:

```sh
docker compose run --rm --no-deps worker python3 bin/backup.py
```

This creates a uniquely named backup **inside the data volume** and prints its
container path. Copy it to independent storage with `docker compose cp`, e.g.
`docker compose cp web:/data/backups/FILE.sqlite ./backups/FILE.sqlite` after
creating `./backups`. Also save `.env` and `config/agents.json` privately, and
back up project workspaces separately. A backup on the same volume is not
disaster recovery. No retention or automatic deletion is configured.

To restore, stop web/worker/telegram, preserve the current data volume as a
backup, and use a **new empty volume**. Copy the chosen snapshot into it as
`llmban.sqlite`, run `init` to set permissions and apply schema, then restart.
Do not overwrite a live WAL database or reuse its old `-wal`/`-shm` files.
Verify cards/runs before re-enabling agents. Runs captured as active may need
explicit recovery after old processes have been confirmed stopped.

## Markdown export

Export all cards or one project without changing cards, events or schema:

```sh
mkdir -p exports
python3 bin/board.py export --output exports/board.md
python3 bin/board.py export --project my-app --output exports/my-app.md
python3 bin/board.py export
```

The native command uses `LLMBAN_DB` (default: `data/llmban.sqlite`). On Windows,
use `python` instead of `python3`. Omit `--project` for all projects, or use
`--project ''` for unassigned cards. An unknown project produces an empty export;
an invalid slug fails. Cards are ordered by workflow column, manual position,
then ID; unknown columns follow known columns alphabetically. IDs, titles,
projects, columns, descriptions and timestamps are included. Descriptions are
literal text blocks so embedded Markdown cannot break the snapshot structure.
Unchanged data produces identical UTF-8 output without an export-time timestamp.

For Docker, rebuild the worker image after upgrading, then write the snapshot
inside the private data volume and copy it to the host:

```sh
docker compose build worker
docker compose run --rm --no-deps worker python3 bin/board.py export --output /data/board.md
docker compose cp web:/data/board.md ./exports/board.md
```

Omit `--output` (or use `--output -`) for stdout. File output requires an existing
parent directory and uses a private temporary file in that directory, followed
by atomic replacement. Failures preserve an existing destination. A missing or
uninitialized database fails instead of being created. No schema migration is
needed; existing SQLite backup and restore commands are unchanged.

Exports contain task content and may contain sensitive information. Keep them
private and do not commit or automatically publish them. The `exports/` directory
is ignored by Git; add an ignore rule for any alternative export location.
Markdown snapshots are for review, not a replacement for SQLite backups.

## Upgrade

1. Review the release notes and back up data/config/workspaces.
2. Stop workers and Telegram; inspect any interrupted work.
3. Pull the reviewed tag/commit and run `docker compose --profile agents build`.
4. Run `docker compose run --rm init` (idempotent schema/permissions bootstrap).
5. Recreate the enabled services with `up -d` and verify the board.

Initial schema version is 1. Keep the backup until you verify the new release;
later schema changes may require restoring a backup rather than downgrading
images. Never use `docker compose down -v` on data you want to keep.

## Isolation and limits

Agents can execute code, consume paid API credits and alter mounted workspaces.
Use dedicated clones, an unprivileged user, restricted credentials and your own
network/resource policy. No Docker socket or host SSH configuration is mounted.
There is one shared board account; it does not provide per-project RBAC.

The default worker environment has only the provider keys and configuration
explicitly passed in Compose; it never receives the board password or Telegram
token. The notifier receives no provider keys or workspace mounts. Containers
share SQLite for this trusted single-instance deployment; this is not isolation
against a malicious coding agent with access to the data volume.

Changing `LLMBAN_WORKSPACES` or UID/GID requires recreating services. Limit the
mount to repositories the instance is allowed to change. To add project runtime
tools, derive a worker image; never enable privileged mode to fix tool access.
