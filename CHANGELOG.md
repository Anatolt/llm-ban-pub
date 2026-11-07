# Changelog

## Unreleased

- Read-only Markdown board export via `bin/board.py export`, with optional
  project filtering, deterministic ordering, UTF-8 stdout and atomic file output.
- Manual card ordering: drop a card between others to place it, within a
  column or across columns. Positions are stored in SQLite, survive reloads
  and restarts, and are kept for cards hidden by a project filter. Stale or
  invalid reorder requests are rejected instead of silently appending.
- Cards moved by the worker and cards created from the CLI now land at the
  bottom of the destination column instead of keeping an arbitrary position.

## 0.1.0

First standalone public release, extracted from LLMBAN's PHP kanban and Python
audit registry, with a new portable worker pipeline.

- Empty SQLite bootstrap, project filters, drag-and-drop cards, English/Russian
  agent output settings, and Current Work coordination view.
- Shared-instance Basic authentication and CSRF-protected mutations.
- Serialized planner/doer/tester agents, configurable argv/workspace mapping,
  bundled Pi adapter, strict JSON result contracts and per-run reports.
- Human review after planning and testing, process-group timeouts, explicit
  interrupted-run recovery, preservation of human edits during runs.
- Optional outbound Telegram summaries with a durable retry outbox.
- Docker Compose profiles, non-root runtime users, native installation guide,
  SQLite backup utility, offline unit/HTTP/pipeline tests and Docker smoke test.

Limits: one trusted team and shared login; no per-user RBAC, untrusted-code
sandbox, live LLM validation in CI, inbound Telegram commands, automatic Git
push/deployment or import of private runtime data.
