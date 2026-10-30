#!/usr/bin/env python3
"""Audit-only shared work registry for LLMBAN phase A."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_DB = os.environ.get("LLMBAN_DB", str(Path(__file__).resolve().parents[1] / "data" / "llmban.sqlite"))
DEFAULT_SNAPSHOT_ROOT = str(Path(DEFAULT_DB).parent / "work")
ACTIVE_STATUSES = {
    "approved",
    "queued",
    "claiming",
    "active",
    "waiting",
    "blocked",
    "verifying",
    "handoff",
    "stale",
}
TERMINAL_STATUSES = {"completed", "canceled"}
PLAN_STATUSES = {"draft", "awaiting_approval"} | ACTIVE_STATUSES | TERMINAL_STATUSES
STEP_STATUSES = {"pending", "in_progress", "completed", "blocked", "unknown", "skipped"}
RESOURCE_TYPES = {
    "host",
    "vm",
    "repo",
    "worktree",
    "branch",
    "path",
    "service",
    "deploy",
    "db",
    "port",
    "external-account",
}
ACCESS_MODES = {"read", "write", "shared-write", "exclusive"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_plans (
    id TEXT PRIMARY KEY,
    task_name TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL,
    harness TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL,
    cwd TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    current_step TEXT NOT NULL DEFAULT '',
    baseline TEXT NOT NULL DEFAULT '',
    recovery_actions TEXT NOT NULL DEFAULT '',
    next_action TEXT NOT NULL DEFAULT '',
    card_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    approved_at TEXT,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_plans_status ON work_plans(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_work_plans_project ON work_plans(project, status);

CREATE TABLE IF NOT EXISTS work_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    acceptance_check TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    evidence TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, step_key),
    FOREIGN KEY(plan_id) REFERENCES work_plans(id)
);
CREATE INDEX IF NOT EXISTS idx_work_steps_plan ON work_steps(plan_id, position, id);

CREATE TABLE IF NOT EXISTS work_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    selector TEXT NOT NULL,
    normalized_selector TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT '',
    baseline TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, resource_type, normalized_selector, access_mode),
    FOREIGN KEY(plan_id) REFERENCES work_plans(id)
);
CREATE INDEX IF NOT EXISTS idx_work_resources_scope
    ON work_resources(resource_type, normalized_selector, plan_id);

CREATE TABLE IF NOT EXISTS work_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_events_plan ON work_events(plan_id, id);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    if not path.startswith("file:"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def emit(data: object, as_json: bool = False, *, minified: bool = False) -> None:
    if not as_json and not minified and isinstance(data, str):
        print(data)
    else:
        options = {"separators": (",", ":")} if minified else {"indent": 2}
        print(json.dumps(data, ensure_ascii=False, **options))


def brief_result(data: object) -> object:
    """Summarize snapshot filenames without changing state or warnings."""
    if not isinstance(data, dict) or not isinstance(data.get("snapshot"), dict):
        return data
    snapshot = dict(data["snapshot"])
    if "active_snapshot_files" not in snapshot:
        return data
    snapshot["active_snapshot_file_count"] = len(snapshot.pop("active_snapshot_files"))
    return {**data, "snapshot": snapshot}


def normalize_resource(resource_type: str, selector: str) -> str:
    value = selector.strip()
    if not value:
        raise ValueError("resource selector must not be empty")
    if resource_type in {"repo", "worktree", "path"}:
        value = value.replace("\\", "/")
        value = re.sub(r"/+", "/", value)
        if len(value) > 1:
            value = value.rstrip("/")
        return value
    if resource_type == "port":
        value = re.sub(r"\s+", "", value.lower())
        return value
    return re.sub(r"\s+", " ", value.lower())


def path_overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def selectors_overlap(a_type: str, a: str, b_type: str, b: str) -> bool:
    path_types = {"repo", "worktree", "path"}
    if a_type in path_types and b_type in path_types:
        return path_overlaps(a, b)
    return a_type == b_type and a == b


def access_conflicts(left: str, right: str) -> bool:
    if left == "read" and right == "read":
        return False
    return True


def active_plan_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    return conn.execute(
        f"SELECT * FROM work_plans WHERE status IN ({marks}) ORDER BY updated_at DESC",
        tuple(sorted(ACTIVE_STATUSES)),
    ).fetchall()


def plan_resources(conn: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM work_resources WHERE plan_id=? ORDER BY id", (plan_id,)
    ).fetchall()


def calculate_conflicts(conn: sqlite3.Connection, plan_id: str, *, active=None, resources_by_plan=None) -> list[dict[str, object]]:
    current = conn.execute("SELECT * FROM work_plans WHERE id=?", (plan_id,)).fetchone()
    if not current:
        raise ValueError(f"plan not found: {plan_id}")
    resources = plan_resources(conn, plan_id)
    conflicts: list[dict[str, object]] = []
    now = utc_now()
    # Load once per comparison pass, rather than one SQL query per peer.
    if resources_by_plan is None:
        resources_by_plan = {}
        for resource in conn.execute("SELECT * FROM work_resources ORDER BY id"):
            resources_by_plan.setdefault(resource["plan_id"], []).append(resource)
    for other in active if active is not None else active_plan_rows(conn):
        if other["id"] == plan_id:
            continue
        other_resources = resources_by_plan.get(other["id"], [])
        overlaps = []
        for left in resources:
            for right in other_resources:
                if selectors_overlap(
                    left["resource_type"],
                    left["normalized_selector"],
                    right["resource_type"],
                    right["normalized_selector"],
                ) and access_conflicts(left["access_mode"], right["access_mode"]):
                    overlaps.append(
                        {
                            "ours": f"{left['resource_type']}:{left['normalized_selector']} ({left['access_mode']})",
                            "theirs": f"{right['resource_type']}:{right['normalized_selector']} ({right['access_mode']})",
                        }
                    )
        lease = datetime.fromisoformat(other["lease_expires_at"])
        stale = lease < now
        if overlaps:
            conflicts.append(
                {
                    "severity": "stale-overlap" if stale else "overlap",
                    "other_plan_id": other["id"],
                    "other_task_name": other["task_name"],
                    "project": other["project"],
                    "owner": other["owner"],
                    "harness": other["harness"],
                    "hostname": other["hostname"],
                    "status": other["status"],
                    "lease_expires_at": other["lease_expires_at"],
                    "overlaps": overlaps,
                }
            )
        elif other["project"] == current["project"]:
            conflicts.append(
                {
                    "severity": "same-project",
                    "other_plan_id": other["id"],
                    "other_task_name": other["task_name"],
                    "owner": other["owner"],
                    "status": other["status"],
                    "lease_expires_at": other["lease_expires_at"],
                    "overlaps": [],
                }
            )
    return conflicts


def log_event(
    conn: sqlite3.Connection,
    plan_id: str | None,
    event_type: str,
    actor: str,
    payload: dict[str, object] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO work_events(plan_id,event_type,actor,payload,created_at) VALUES (?,?,?,?,?)",
        (plan_id, event_type, actor, json.dumps(payload or {}, ensure_ascii=False), iso()),
    )


def get_plan(conn: sqlite3.Connection, reference: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM work_plans WHERE id=? OR task_name=?", (reference, reference)
    ).fetchone()
    if not row:
        raise ValueError(f"plan not found: {reference}")
    return row


def parse_result_links(values: list[str]) -> list[dict[str, str]]:
    if len(values) > 10:
        raise ValueError("at most 10 --result-link values are allowed")
    links = []
    seen = set()
    for raw in values:
        value = raw.strip()
        if value.startswith("https://"):
            label, url = "", value
        elif "=" in value:
            label, url = (part.strip() for part in value.split("=", 1))
            if not label:
                raise ValueError("result link label must not be empty")
        else:
            raise ValueError("result link must be HTTPS_URL or LABEL=HTTPS_URL")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("result link must be an HTTPS URL without embedded credentials")
        if len(label) > 120 or len(url) > 2048:
            raise ValueError("result link label or URL is too long")
        key = (label, url)
        if key not in seen:
            links.append({"label": label, "url": url})
            seen.add(key)
    return links


def atomic_write(path: Path, content: str) -> None:
    # Avoid fsync/replace for unchanged generated snapshots.
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_snapshot_name(task_name: str, plan_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_name.lower()).strip("-")[:64]
    return f"{slug or 'task'}-{plan_id[:8]}.md"


def render_plan(conn: sqlite3.Connection, plan: sqlite3.Row, *, conflicts=None) -> str:
    resources = plan_resources(conn, plan["id"])
    steps = conn.execute(
        "SELECT * FROM work_steps WHERE plan_id=? ORDER BY position,id", (plan["id"],)
    ).fetchall()
    if conflicts is None:
        conflicts = calculate_conflicts(conn, plan["id"]) if plan["status"] in ACTIVE_STATUSES else []
    lines = [
        f"# {plan['task_name']}",
        "",
        "> Generated by memctl. Do not edit manually.",
        "",
        f"- Plan ID: `{plan['id']}`",
        f"- Project: `{plan['project']}`",
        f"- Status: `{plan['status']}`",
        f"- Owner: `{plan['owner']}` via `{plan['harness']}`",
        f"- Thread: `{plan['thread_id'] or '-'}`",
        f"- Location: `{plan['hostname']}:{plan['cwd']}`",
        f"- Current step: {plan['current_step'] or '-'}",
        f"- Next action: {plan['next_action'] or '-'}",
        f"- Heartbeat: `{plan['heartbeat_at']}`",
        f"- Lease expires: `{plan['lease_expires_at']}`",
        "",
        "## Goal",
        "",
        plan["goal"] or "-",
        "",
        "## Resources",
        "",
    ]
    lines.extend(
        f"- `{r['access_mode']}` `{r['resource_type']}:{r['normalized_selector']}` — {r['operation'] or '-'}; baseline: `{r['baseline'] or '-'}`"
        for r in resources
    )
    if not resources:
        lines.append("- None declared")
    lines += ["", "## Steps", ""]
    lines.extend(
        f"- [{'x' if s['status'] == 'completed' else ' '}] `{s['status']}` {s['step_key']}: {s['title']}"
        + (f" — evidence: {s['evidence']}" if s["evidence"] else "")
        for s in steps
    )
    if not steps:
        lines.append("- No structured steps")
    lines += ["", "## Coordination warnings", ""]
    lines.extend(
        f"- `{c['severity']}` with `{c['other_task_name']}` ({c['owner']}, {c['status']})"
        for c in conflicts
    )
    if not conflicts:
        lines.append("- None")
    lines += ["", "## Recovery", "", plan["recovery_actions"] or "-", ""]
    return "\n".join(lines)


def render_snapshots(conn: sqlite3.Connection, root: str) -> dict[str, object]:
    root_path = Path(root).resolve()
    active_path = root_path / "active"
    archive_path = root_path / "archive"
    active_path.mkdir(parents=True, exist_ok=True)
    archive_path.mkdir(parents=True, exist_ok=True)
    active = active_plan_rows(conn)
    resources_by_plan = {}
    for resource in conn.execute("SELECT * FROM work_resources ORDER BY id"):
        resources_by_plan.setdefault(resource["plan_id"], []).append(resource)
    warnings_by_plan = {p["id"]: calculate_conflicts(conn, p["id"], active=active, resources_by_plan=resources_by_plan) for p in active}
    active_files: set[str] = set()
    for plan in active:
        filename = safe_snapshot_name(plan["task_name"], plan["id"])
        active_files.add(filename)
        atomic_write(active_path / filename, render_plan(conn, plan, conflicts=warnings_by_plan[plan["id"]]))
    terminal_marks = ",".join("?" for _ in TERMINAL_STATUSES)
    terminal = conn.execute(
        f"SELECT * FROM work_plans WHERE status IN ({terminal_marks}) ORDER BY updated_at DESC",
        tuple(sorted(TERMINAL_STATUSES)),
    ).fetchall()
    for plan in terminal:
        filename = safe_snapshot_name(plan["task_name"], plan["id"])
        old_active = active_path / filename
        if old_active.exists():
            previous = archive_path / f"{old_active.stem}-before-close-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.md"
            os.replace(old_active, previous)
        atomic_write(archive_path / filename, render_plan(conn, plan))
    lines = [
        "# Active agent work",
        "",
        "> Generated by memctl. LLMBAN Work Registry is the live source of truth.",
        "",
        f"Updated: `{iso()}`",
        "",
    ]
    if active:
        for plan in active:
            filename = safe_snapshot_name(plan["task_name"], plan["id"])
            warnings = warnings_by_plan[plan["id"]]
            lines.append(
                f"- [{plan['task_name']}](active/{filename}) — `{plan['status']}`; "
                f"{plan['owner']} / {plan['harness']} on `{plan['hostname']}`; "
                f"step: {plan['current_step'] or '-'}; warnings: {len(warnings)}"
            )
    else:
        lines.append("- No active plans")
    lines.append("")
    atomic_write(root_path / "ACTIVE.md", "\n".join(lines))
    return {
        "active_plans": len(active),
        "active_snapshot_files": sorted(active_files),
        "archived_plans": len(terminal),
    }


def command_init(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    ensure_schema(conn)
    return {"ok": True, "db": args.db, "mode": "audit-only"}


def command_register(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    if args.status not in PLAN_STATUSES:
        raise ValueError(f"invalid status: {args.status}")
    now = utc_now()
    if args.lease_minutes <= 0:
        raise ValueError("lease minutes must be positive")
    metadata = json.dumps(json.loads(args.metadata), ensure_ascii=False)
    plan_id = args.id or str(uuid.uuid4())
    lease = now + timedelta(minutes=args.lease_minutes)
    approved_at = iso(now) if args.status not in {"draft", "awaiting_approval"} else None
    started_at = iso(now) if args.status in ACTIVE_STATUSES else None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """INSERT INTO work_plans(
            id,task_name,project,goal,acceptance_criteria,owner,harness,thread_id,
            hostname,cwd,repo,worktree,branch,status,current_step,baseline,
            recovery_actions,next_action,card_id,metadata_json,created_at,approved_at,
            started_at,updated_at,heartbeat_at,lease_expires_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            plan_id,
            args.task_name,
            args.project,
            args.goal,
            args.acceptance,
            args.owner,
            args.harness,
            args.thread_id,
            args.hostname,
            args.cwd,
            args.repo,
            args.worktree,
            args.branch,
            args.status,
            args.current_step,
            args.baseline,
            args.recovery,
            args.next_action,
            args.card_id,
            metadata,
            iso(now),
            approved_at,
            started_at,
            iso(now),
            iso(now),
            iso(lease),
        ),
    )
    log_event(conn, plan_id, "plan_registered", args.owner, {"mode": "audit-only"})
    conn.commit()
    snapshot = render_snapshots(conn, args.snapshot_root)
    return {"ok": True, "plan_id": plan_id, "task_name": args.task_name, "snapshot": snapshot}


def parse_boot_resource(value: str) -> tuple[str, str]:
    resource_type, separator, selector = value.partition(":")
    if not separator or resource_type not in RESOURCE_TYPES:
        choices = ", ".join(sorted(RESOURCE_TYPES))
        raise ValueError(f"resource must be TYPE:SELECTOR; TYPE must be one of: {choices}")
    return resource_type, normalize_resource(resource_type, selector)


def boot_plan_matches(conn: sqlite3.Connection, row: sqlite3.Row, args: argparse.Namespace) -> bool:
    matches: list[bool] = []
    if args.project:
        matches.append(row["project"] == args.project)
    if args.hostname:
        matches.append(row["hostname"] == args.hostname)

    resources = plan_resources(conn, row["id"])
    for candidate in (args.repo, args.cwd):
        if not candidate:
            continue
        normalized = normalize_resource("path", candidate)
        declared_paths = [row["repo"], row["worktree"], row["cwd"]]
        path_match = any(
            path_overlaps(normalized, normalize_resource("path", path))
            for path in declared_paths
            if path
        )
        resource_match = any(
            resource["resource_type"] in {"repo", "worktree", "path"}
            and path_overlaps(normalized, resource["normalized_selector"])
            for resource in resources
        )
        matches.append(path_match or resource_match)

    for value in args.resource:
        resource_type, selector = parse_boot_resource(value)
        matches.append(
            any(
                selectors_overlap(
                    resource_type,
                    selector,
                    declared["resource_type"],
                    declared["normalized_selector"],
                )
                for declared in resources
            )
        )
    return not matches or any(matches)


def compact_lease_state(row: sqlite3.Row) -> str:
    now = utc_now()
    if datetime.fromisoformat(row["lease_expires_at"]) >= now:
        return "live"
    if datetime.fromisoformat(row["updated_at"]) < now - timedelta(days=7):
        return "long-stale"
    return "expired"


def serialize_compact_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    warnings = calculate_conflicts(conn, row["id"])
    severities: dict[str, int] = {}
    for warning in warnings:
        severity = str(warning["severity"])
        severities[severity] = severities.get(severity, 0) + 1
    data = {
        key: row[key]
        for key in (
            "id",
            "task_name",
            "project",
            "owner",
            "harness",
            "hostname",
            "cwd",
            "repo",
            "worktree",
            "branch",
            "status",
            "current_step",
            "updated_at",
            "heartbeat_at",
            "lease_expires_at",
        )
        if row[key] not in (None, "")
    }
    data["lease_state"] = compact_lease_state(row)
    data["resources"] = [
        [
            resource["resource_type"],
            resource["normalized_selector"],
            resource["access_mode"],
        ]
        for resource in plan_resources(conn, row["id"])
    ]
    data["warning_count"] = len(warnings)
    if severities:
        data["warning_severities"] = severities
    return data


def command_boot(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    all_rows = active_plan_rows(conn)
    rows = [row for row in all_rows if boot_plan_matches(conn, row, args)]
    common = {
        "mode": "audit-only",
        "source_of_truth": {
            "durable_knowledge": os.environ.get("LLMBAN_KNOWLEDGE_ROOT", ""),
            "active_work": args.db,
            "bootstrap": os.environ.get("LLMBAN_BOOTSTRAP", ""),
            "snapshots": args.snapshot_root,
        },
        "required_before_mutation": [
            "formulate a named plan; an explicit direct reversible in-scope request already authorizes it",
            "obtain separate approval only for ambiguity, scope expansion, conflicts, or high-impact/irreversible actions",
            "register the plan",
            "declare write intents",
            "review overlap warnings",
            "run and record factual preflight",
        ],
    }
    if args.full:
        common["plans"] = [serialize_plan(conn, row) for row in rows]
        return common
    common.update(
        {
            "format": "compact",
            "selection": {
                "match": "any",
                "total_unfinished": len(all_rows),
                "returned": len(rows),
                "filters": {
                    key: value
                    for key, value in {
                        "project": args.project,
                        "hostname": args.hostname,
                        "repo": args.repo,
                        "cwd": args.cwd,
                        "resource": args.resource,
                    }.items()
                    if value not in (None, "", [])
                },
            },
            "detail_commands": {
                "plan": "memctl --json show --plan PLAN",
                "conflicts": "memctl --json conflicts --plan PLAN",
                "all_fields": "memctl --json boot --full",
            },
            "resource_fields": ["type", "selector", "access"],
            "plans": [serialize_compact_plan(conn, row) for row in rows],
        }
    )
    return common


def command_intent(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    if args.resource_type not in RESOURCE_TYPES:
        raise ValueError(f"invalid resource type: {args.resource_type}")
    if args.access_mode not in ACCESS_MODES:
        raise ValueError(f"invalid access mode: {args.access_mode}")
    plan = get_plan(conn, args.plan)
    normalized = normalize_resource(args.resource_type, args.selector)
    now = iso()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """INSERT INTO work_resources(
            plan_id,resource_type,selector,normalized_selector,access_mode,
            operation,baseline,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(plan_id,resource_type,normalized_selector,access_mode)
        DO UPDATE SET selector=excluded.selector,operation=excluded.operation,
                      baseline=excluded.baseline,updated_at=excluded.updated_at""",
        (
            plan["id"], args.resource_type, args.selector, normalized,
            args.access_mode, args.operation, args.baseline, now, now,
        ),
    )
    conflicts = calculate_conflicts(conn, plan["id"])
    log_event(
        conn,
        plan["id"],
        "intent_declared",
        args.actor or plan["owner"],
        {
            "resource_type": args.resource_type,
            "selector": normalized,
            "access_mode": args.access_mode,
            "warnings": conflicts,
            "enforced": False,
        },
    )
    conn.commit()
    snapshot = render_snapshots(conn, args.snapshot_root)
    return {
        "ok": True,
        "mode": "audit-only",
        "plan_id": plan["id"],
        "resource": f"{args.resource_type}:{normalized}",
        "warnings": conflicts,
        "mutation_blocked": False,
        "snapshot": snapshot,
    }


def command_step(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    if args.status not in STEP_STATUSES:
        raise ValueError(f"invalid step status: {args.status}")
    plan = get_plan(conn, args.plan)
    now = iso()
    started = now if args.status == "in_progress" else None
    completed = now if args.status in {"completed", "skipped"} else None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """INSERT INTO work_steps(
            plan_id,step_key,position,title,acceptance_check,status,evidence,
            started_at,completed_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(plan_id,step_key) DO UPDATE SET
            position=excluded.position,title=excluded.title,
            acceptance_check=excluded.acceptance_check,status=excluded.status,
            evidence=excluded.evidence,
            started_at=COALESCE(work_steps.started_at,excluded.started_at),
            completed_at=excluded.completed_at,updated_at=excluded.updated_at""",
        (
            plan["id"], args.step_key, args.position, args.title,
            args.acceptance, args.status, args.evidence, started, completed, now,
        ),
    )
    conn.execute(
        "UPDATE work_plans SET current_step=?,next_action=?,updated_at=? WHERE id=?",
        (args.title if args.status == "in_progress" else plan["current_step"], args.next_action, now, plan["id"]),
    )
    log_event(conn, plan["id"], f"step_{args.status}", args.actor or plan["owner"], {"step_key": args.step_key, "evidence": args.evidence})
    conn.commit()
    return {"ok": True, "plan_id": plan["id"], "step_key": args.step_key, "status": args.status, "snapshot": render_snapshots(conn, args.snapshot_root)}


def command_heartbeat(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    plan = get_plan(conn, args.plan)
    if args.lease_minutes <= 0:
        raise ValueError("lease minutes must be positive")
    now = utc_now()
    lease = now + timedelta(minutes=args.lease_minutes)
    revived = plan["status"] == "stale"
    status = "active" if revived else plan["status"]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE work_plans SET status=?,heartbeat_at=?,lease_expires_at=?,updated_at=? WHERE id=?",
        (status, iso(now), iso(lease), iso(now), plan["id"]),
    )
    if revived:
        log_event(
            conn,
            plan["id"],
            "status_changed",
            args.actor or plan["owner"],
            {"from": "stale", "to": "active", "reason": "heartbeat_recovered"},
        )
    log_event(conn, plan["id"], "heartbeat", args.actor or plan["owner"], {"lease_minutes": args.lease_minutes})
    conn.commit()
    return {"ok": True, "plan_id": plan["id"], "lease_expires_at": iso(lease), "snapshot": render_snapshots(conn, args.snapshot_root)}


def command_preflight(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    plan = get_plan(conn, args.plan)
    mismatches = []
    if args.observed_hostname != plan["hostname"]:
        mismatches.append({"field": "hostname", "declared": plan["hostname"], "observed": args.observed_hostname})
    if args.observed_cwd != plan["cwd"]:
        mismatches.append({"field": "cwd", "declared": plan["cwd"], "observed": args.observed_cwd})
    if plan["baseline"] and args.observed_baseline and plan["baseline"] != args.observed_baseline:
        mismatches.append({"field": "baseline", "declared": plan["baseline"], "observed": args.observed_baseline})
    warnings = calculate_conflicts(conn, plan["id"])
    payload = {
        "observed_hostname": args.observed_hostname,
        "observed_cwd": args.observed_cwd,
        "observed_baseline": args.observed_baseline,
        "git_status": args.git_status,
        "observed_state": args.observed_state,
        "mismatches": mismatches,
        "coordination_warnings": warnings,
        "enforced": False,
    }
    conn.execute("BEGIN IMMEDIATE")
    log_event(conn, plan["id"], "preflight_recorded", args.actor or plan["owner"], payload)
    conn.execute("UPDATE work_plans SET updated_at=? WHERE id=?", (iso(), plan["id"]))
    conn.commit()
    return {
        "ok": True,
        "mode": "audit-only",
        "plan_id": plan["id"],
        "preflight_ok": not mismatches and not args.git_status.strip(),
        "mismatches": mismatches,
        "dirty_worktree": bool(args.git_status.strip()),
        "warnings": warnings,
        "mutation_blocked": False,
    }


def command_status(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    if args.status not in PLAN_STATUSES:
        raise ValueError(f"invalid status: {args.status}")
    plan = get_plan(conn, args.plan)
    result_links = None
    metadata = plan["metadata_json"]
    if args.result_link:
        if args.status != "completed":
            raise ValueError("--result-link is only valid with --status completed")
        result_links = parse_result_links(args.result_link)
        metadata_object = json.loads(metadata or "{}")
        if not isinstance(metadata_object, dict):
            raise ValueError("plan metadata must be a JSON object")
        metadata_object["result_links"] = result_links
        metadata_object.pop("result_url", None)
        metadata = json.dumps(metadata_object, ensure_ascii=False)
    now = iso()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE work_plans SET status=?,current_step=?,next_action=?,recovery_actions=?,metadata_json=?,updated_at=? WHERE id=?",
        (
            args.status,
            args.current_step if args.current_step is not None else plan["current_step"],
            args.next_action if args.next_action is not None else plan["next_action"],
            args.recovery if args.recovery is not None else plan["recovery_actions"],
            metadata,
            now,
            plan["id"],
        ),
    )
    log_event(conn, plan["id"], "status_changed", args.actor or plan["owner"], {"from": plan["status"], "to": args.status})
    conn.commit()
    return {"ok": True, "plan_id": plan["id"], "status": args.status, "snapshot": render_snapshots(conn, args.snapshot_root)}


def serialize_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    data = dict(row)
    data["resources"] = [dict(r) for r in plan_resources(conn, row["id"])]
    data["steps"] = [dict(s) for s in conn.execute("SELECT * FROM work_steps WHERE plan_id=? ORDER BY position,id", (row["id"],))]
    data["warnings"] = calculate_conflicts(conn, row["id"]) if row["status"] in ACTIVE_STATUSES else []
    return data


def command_list(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    if args.all:
        rows = conn.execute("SELECT * FROM work_plans ORDER BY updated_at DESC").fetchall()
    else:
        rows = active_plan_rows(conn)
    return {"mode": "audit-only", "plans": [serialize_plan(conn, row) for row in rows]}


def command_show(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    return serialize_plan(conn, get_plan(conn, args.plan))


def command_conflicts(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    plan = get_plan(conn, args.plan)
    return {"mode": "audit-only", "plan_id": plan["id"], "warnings": calculate_conflicts(conn, plan["id"]), "mutation_blocked": False}


def command_snapshot(args: argparse.Namespace, conn: sqlite3.Connection) -> dict[str, object]:
    return {"ok": True, "snapshot": render_snapshots(conn, args.snapshot_root)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LLMBAN shared work registry (phase A, audit-only)")
    parser.add_argument("--db", default=os.environ.get("MEMCTL_DB", DEFAULT_DB))
    parser.add_argument("--snapshot-root", default=os.environ.get("MEMCTL_SNAPSHOT_ROOT", DEFAULT_SNAPSHOT_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-min", action="store_true", help="emit JSON without indentation (implies --json)")
    parser.add_argument("--brief", action="store_true", help="replace snapshot filenames with a count; preserve warnings")
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("init")
    boot = subs.add_parser("boot")
    boot.add_argument("--full", action="store_true", help="return the legacy detailed plan schema")
    boot.add_argument("--project")
    boot.add_argument("--hostname")
    boot.add_argument("--repo")
    boot.add_argument("--cwd")
    boot.add_argument(
        "--resource",
        action="append",
        default=[],
        metavar="TYPE:SELECTOR",
        help="return plans with an overlapping declared resource; repeatable",
    )

    reg = subs.add_parser("register")
    reg.add_argument("--id")
    reg.add_argument("--task-name", required=True)
    reg.add_argument("--project", required=True)
    reg.add_argument("--goal", default="")
    reg.add_argument("--acceptance", default="")
    reg.add_argument("--owner", required=True)
    reg.add_argument("--harness", required=True)
    reg.add_argument("--thread-id", default="")
    reg.add_argument("--hostname", required=True)
    reg.add_argument("--cwd", required=True)
    reg.add_argument("--repo", default="")
    reg.add_argument("--worktree", default="")
    reg.add_argument("--branch", default="")
    reg.add_argument("--status", default="active")
    reg.add_argument("--current-step", default="")
    reg.add_argument("--baseline", default="")
    reg.add_argument("--recovery", default="")
    reg.add_argument("--next-action", default="")
    reg.add_argument("--card-id", type=int)
    reg.add_argument("--metadata", default="{}")
    reg.add_argument("--lease-minutes", type=int, default=30)

    intent = subs.add_parser("intent")
    intent.add_argument("--plan", required=True)
    intent.add_argument("--resource-type", required=True, choices=sorted(RESOURCE_TYPES))
    intent.add_argument("--selector", required=True)
    intent.add_argument("--access-mode", default="write", choices=sorted(ACCESS_MODES))
    intent.add_argument("--operation", default="")
    intent.add_argument("--baseline", default="")
    intent.add_argument("--actor", default="")

    step = subs.add_parser("step")
    step.add_argument("--plan", required=True)
    step.add_argument("--step-key", required=True)
    step.add_argument("--position", type=int, default=0)
    step.add_argument("--title", required=True)
    step.add_argument("--acceptance", default="")
    step.add_argument("--status", required=True, choices=sorted(STEP_STATUSES))
    step.add_argument("--evidence", default="")
    step.add_argument("--next-action", default="")
    step.add_argument("--actor", default="")

    heartbeat = subs.add_parser("heartbeat")
    heartbeat.add_argument("--plan", required=True)
    heartbeat.add_argument("--lease-minutes", type=int, default=30)
    heartbeat.add_argument("--actor", default="")

    preflight = subs.add_parser("preflight")
    preflight.add_argument("--plan", required=True)
    preflight.add_argument("--observed-hostname", required=True)
    preflight.add_argument("--observed-cwd", required=True)
    preflight.add_argument("--observed-baseline", default="")
    preflight.add_argument("--git-status", default="")
    preflight.add_argument("--observed-state", default="")
    preflight.add_argument("--actor", default="")

    status = subs.add_parser("status")
    status.add_argument("--plan", required=True)
    status.add_argument("--status", required=True, choices=sorted(PLAN_STATUSES))
    status.add_argument("--current-step")
    status.add_argument("--next-action")
    status.add_argument("--recovery")
    status.add_argument(
        "--result-link",
        action="append",
        metavar="[LABEL=]HTTPS_URL",
        help="public result URL; repeat for multiple named results (completed status only)",
    )
    status.add_argument("--actor", default="")

    listing = subs.add_parser("list")
    listing.add_argument("--all", action="store_true")
    show = subs.add_parser("show")
    show.add_argument("--plan", required=True)
    conflicts = subs.add_parser("conflicts")
    conflicts.add_argument("--plan", required=True)
    subs.add_parser("snapshot")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    conn = None
    try:
        conn = connect(args.db)
        ensure_schema(conn)
        handlers = {
            "init": command_init,
            "boot": command_boot,
            "register": command_register,
            "intent": command_intent,
            "step": command_step,
            "heartbeat": command_heartbeat,
            "preflight": command_preflight,
            "status": command_status,
            "list": command_list,
            "show": command_show,
            "conflicts": command_conflicts,
            "snapshot": command_snapshot,
        }
        result = handlers[args.command](args, conn)
        emit(brief_result(result) if args.brief else result, args.json, minified=args.json_min)
        return 0
    except (ValueError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        emit({"ok": False, "error": str(exc)}, True, minified=args.json_min)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
