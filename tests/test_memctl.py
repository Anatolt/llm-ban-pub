from __future__ import annotations

import copy
from contextlib import closing
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import sys
from unittest import mock
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "bin" / "memctl.py"
SPEC = importlib.util.spec_from_file_location("memctl", MODULE_PATH)
memctl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(memctl)


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "llmban.sqlite")
        self.conn = memctl.connect(self.db)
        memctl.ensure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def add_plan(self, plan_id: str, name: str, project: str = "demo") -> None:
        now = memctl.iso()
        self.conn.execute(
            """INSERT INTO work_plans(
              id,task_name,project,owner,harness,hostname,cwd,status,
              created_at,updated_at,heartbeat_at,lease_expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, name, project, "agent", "codex", "host", "/repo", "active", now, now, now, now),
        )
        self.conn.commit()

    def add_resource(self, plan_id: str, kind: str, selector: str, mode: str = "write") -> None:
        now = memctl.iso()
        normalized = memctl.normalize_resource(kind, selector)
        self.conn.execute(
            """INSERT INTO work_resources(
              plan_id,resource_type,selector,normalized_selector,access_mode,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (plan_id, kind, selector, normalized, mode, now, now),
        )
        self.conn.commit()

    def boot_args(self, **overrides: object) -> SimpleNamespace:
        values = {
            "db": self.db,
            "snapshot_root": str(Path(self.tmp.name) / "work"),
            "full": False,
            "project": None,
            "hostname": None,
            "repo": None,
            "cwd": None,
            "resource": [],
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_path_normalization_and_overlap(self) -> None:
        self.assertEqual(memctl.normalize_resource("path", "C:\\repo\\src\\"), "C:/repo/src")
        self.assertTrue(memctl.path_overlaps("/repo", "/repo/src/a.py"))
        self.assertFalse(memctl.path_overlaps("/repo-a", "/repo-b"))

    def test_write_overlap_is_reported(self) -> None:
        self.add_plan("a", "first")
        self.add_plan("b", "second")
        self.add_resource("a", "path", "/repo/src")
        self.add_resource("b", "path", "/repo/src/app.py")
        warnings = memctl.calculate_conflicts(self.conn, "b")
        self.assertEqual(warnings[0]["severity"], "stale-overlap")
        self.assertEqual(warnings[0]["other_plan_id"], "a")

    def test_read_read_does_not_overlap(self) -> None:
        self.add_plan("a", "first")
        self.add_plan("b", "second")
        self.add_resource("a", "service", "nginx", "read")
        self.add_resource("b", "service", "NGINX", "read")
        warnings = memctl.calculate_conflicts(self.conn, "b")
        self.assertEqual(warnings[0]["severity"], "same-project")

    def test_different_project_without_overlap_is_quiet(self) -> None:
        self.add_plan("a", "first", "one")
        self.add_plan("b", "second", "two")
        self.add_resource("a", "service", "nginx")
        self.add_resource("b", "service", "postgres")
        self.assertEqual(memctl.calculate_conflicts(self.conn, "b"), [])

    def test_unchanged_snapshot_skips_fsync(self) -> None:
        path = Path(self.tmp.name) / "unchanged.md"
        memctl.atomic_write(path, "same")
        with mock.patch.object(memctl.os, "fsync", side_effect=AssertionError("unexpected rewrite")):
            memctl.atomic_write(path, "same")
        self.assertEqual(path.read_text(), "same")
        memctl.atomic_write(path, "changed")
        self.assertEqual(path.read_text(), "changed")

    def test_conflicts_load_peer_resources_in_one_query(self) -> None:
        for i in range(8):
            self.add_plan(str(i), "task-" + str(i))
            self.add_resource(str(i), "path", "/shared")
        queries = []
        self.conn.set_trace_callback(queries.append)
        conflicts = memctl.calculate_conflicts(self.conn, "0")
        self.conn.set_trace_callback(None)
        self.assertEqual(len(conflicts), 7)
        resource_reads = [q for q in queries if "SELECT * FROM work_resources" in q]
        self.assertEqual(len(resource_reads), 2)

    def test_completed_snapshot_moves_out_of_active(self) -> None:
        self.add_plan("a", "first")
        root = Path(self.tmp.name) / "work"
        memctl.render_snapshots(self.conn, str(root))
        active = root / "active" / "first-a.md"
        self.assertTrue(active.exists())
        self.conn.execute("UPDATE work_plans SET status='completed' WHERE id='a'")
        self.conn.commit()
        memctl.render_snapshots(self.conn, str(root))
        self.assertFalse(active.exists())
        self.assertTrue((root / "archive" / "first-a.md").exists())

    def test_compact_boot_omits_heavy_fields_and_full_preserves_them(self) -> None:
        self.add_plan("a", "first")
        self.add_resource("a", "path", "/repo/src")
        compact = memctl.command_boot(self.boot_args(), self.conn)
        plan = compact["plans"][0]
        self.assertEqual(compact["format"], "compact")
        self.assertNotIn("steps", plan)
        self.assertNotIn("warnings", plan)
        self.assertEqual(compact["resource_fields"], ["type", "selector", "access"])
        self.assertEqual(plan["resources"][0], ["path", "/repo/src", "write"])

        full = memctl.command_boot(self.boot_args(full=True), self.conn)
        self.assertNotIn("format", full)
        self.assertIn("steps", full["plans"][0])
        self.assertIn("warnings", full["plans"][0])
        self.assertIn("goal", full["plans"][0])

    def test_boot_resource_filter_uses_overlap_rules(self) -> None:
        self.add_plan("a", "source")
        self.add_plan("b", "service", "other")
        self.add_resource("a", "path", "/repo/src")
        self.add_resource("b", "service", "nginx")
        data = memctl.command_boot(
            self.boot_args(resource=["path:/repo/src/app.py"]), self.conn
        )
        self.assertEqual([plan["id"] for plan in data["plans"]], ["a"])
        self.assertEqual(data["selection"]["total_unfinished"], 2)
        self.assertEqual(data["selection"]["returned"], 1)

    def test_boot_filters_are_combined_as_safe_union(self) -> None:
        self.add_plan("a", "project-match", "one")
        self.add_plan("b", "resource-match", "two")
        self.add_plan("c", "unrelated", "three")
        self.add_resource("b", "service", "nginx")
        data = memctl.command_boot(
            self.boot_args(project="one", resource=["service:nginx"]), self.conn
        )
        self.assertEqual({plan["id"] for plan in data["plans"]}, {"a", "b"})

    def test_invalid_boot_resource_is_rejected(self) -> None:
        self.add_plan("a", "first")
        with self.assertRaisesRegex(ValueError, "TYPE:SELECTOR"):
            memctl.command_boot(self.boot_args(resource=["missing-colon"]), self.conn)


class OutputTests(unittest.TestCase):
    NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cli(self, root, flags, command, expected_code=0):
        output = StringIO()
        argv = ["memctl", "--db", str(root / "db.sqlite"),
                "--snapshot-root", str(root / "work"), *flags, *command]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(memctl, "utc_now", return_value=self.NOW), \
                redirect_stdout(output):
            self.assertEqual(memctl.main(), expected_code)
        raw = output.getvalue()
        if "--json-min" in flags:
            self.assertEqual(raw.count("\n"), 1)
        return json.loads(raw), raw

    def seed(self, root, count):
        root.mkdir()
        conn = memctl.connect(str(root / "db.sqlite"))
        memctl.ensure_schema(conn)
        old = "2026-09-01T00:00:00+00:00"
        for index in range(count):
            conn.execute(
                """INSERT INTO work_plans(id,task_name,project,owner,harness,
                hostname,cwd,status,created_at,updated_at,heartbeat_at,lease_expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"peer-{index}", f"peer-task-{index}", "other", "peer-owner",
                 "codex", "host", "/repo", "handoff", old, old, old, old),
            )
        if count:
            conn.execute(
                """INSERT INTO work_resources(plan_id,resource_type,selector,
                normalized_selector,access_mode,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""",
                ("peer-0", "path", "/repo/данные", "/repo/данные", "write", old, old),
            )
        conn.commit()
        conn.close()

    def state(self, root):
        with closing(sqlite3.connect(root / "db.sqlite")) as conn:
            db = {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                  for table in ("work_plans", "work_resources", "work_steps", "work_events")}
        files = {str(p.relative_to(root / "work")): p.read_bytes()
                 for p in (root / "work").rglob("*.md")}
        return db, files

    def test_modes_preserve_warnings_events_and_snapshots(self):
        commands = [
            ["init"],
            ["register", "--id", "ours", "--task-name", "аудит", "--project", "demo",
             "--owner", "agent", "--harness", "codex", "--hostname", "host",
             "--cwd", "/repo", "--baseline", "baseline", "--status", "active"],
            ["intent", "--plan", "ours", "--resource-type", "path", "--selector",
             "/repo/данные/файл.py", "--access-mode", "write"],
            ["intent", "--plan", "ours", "--resource-type", "service", "--selector", "demo"],
            ["step", "--plan", "ours", "--step-key", "test", "--title", "Verify",
             "--status", "in_progress"],
            ["heartbeat", "--plan", "ours"],
            ["preflight", "--plan", "ours", "--observed-hostname", "wrong",
             "--observed-cwd", "/wrong", "--observed-baseline", "wrong",
             "--git-status", "?? файл.py"],
            ["step", "--plan", "ours", "--step-key", "test", "--title", "Verify",
             "--status", "completed", "--evidence", "checks passed"],
            ["status", "--plan", "ours", "--status", "completed"],
            ["snapshot"],
        ]
        modes = [["--json"], ["--json-min"], ["--json", "--brief"],
                 ["--json-min", "--brief"]]
        for count in (0, 1, 55):
            with self.subTest(plans=count):
                baseline = []
                baseline_state = None
                for index, flags in enumerate(modes):
                    root = self.root / f"{count}-{index}"
                    self.seed(root, count)
                    for position, command in enumerate(commands):
                        actual, raw = self.cli(root, flags, command)
                        if index == 0:
                            baseline.append(actual)
                        else:
                            expected = copy.deepcopy(baseline[position])
                            if command[0] == "init":
                                expected["db"] = str(root / "db.sqlite")
                            if "--brief" in flags and "snapshot" in expected:
                                files = expected["snapshot"].pop("active_snapshot_files")
                                expected["snapshot"]["active_snapshot_file_count"] = len(files)
                            self.assertEqual(actual, expected)
                        if command[0] == "intent" and count:
                            warning = actual["warnings"][0]
                            self.assertEqual(warning["severity"], "stale-overlap")
                            self.assertEqual(warning["owner"], "peer-owner")
                            self.assertTrue(warning["overlaps"])
                            self.assertIn("lease_expires_at", warning)
                        if command[0] == "preflight":
                            self.assertFalse(actual["preflight_ok"])
                            self.assertTrue(actual["dirty_worktree"])
                            self.assertEqual(len(actual["mismatches"]), 3)
                        if command[0] == "register":
                            self.assertIn("аудит", raw)
                    if index == 0:
                        baseline_state = self.state(root)
                    else:
                        self.assertEqual(self.state(root), baseline_state)

    def test_read_formats_preserve_full_schema_and_empty_boot(self):
        root = self.root / "read"
        self.seed(root, 1)
        for command in (["boot"], ["boot", "--full"], ["boot", "--project", "missing"],
                        ["show", "--plan", "peer-0"], ["list"],
                        ["conflicts", "--plan", "peer-0"]):
            expected, pretty = self.cli(root, ["--json"], command)
            for flags in (["--json-min"], ["--json-min", "--brief"]):
                actual, compact = self.cli(root, flags, command)
                self.assertEqual(actual, expected)
                self.assertLess(len(compact), len(pretty))
        full, _ = self.cli(root, ["--json-min"], ["boot", "--full"])
        self.assertIn("steps", full["plans"][0])
        empty, _ = self.cli(root, ["--json-min"], ["boot", "--project", "missing"])
        self.assertEqual(empty["selection"]["returned"], 0)

    def test_completed_status_records_named_result_links(self):
        root = self.root / "results"
        self.seed(root, 0)
        self.cli(root, ["--json-min"], [
            "register", "--id", "ours", "--task-name", "task", "--project", "demo",
            "--owner", "agent", "--harness", "codex", "--hostname", "host", "--cwd", "/repo",
        ])
        self.cli(root, ["--json-min"], [
            "status", "--plan", "ours", "--status", "completed",
            "--result-link", "Т-Банк=https://example.com/t-bank",
            "--result-link", "Точка=https://example.com/tochka-bank",
        ])
        shown, _ = self.cli(root, ["--json-min"], ["show", "--plan", "ours"])
        self.assertEqual(json.loads(shown["metadata_json"])["result_links"], [
            {"label": "Т-Банк", "url": "https://example.com/t-bank"},
            {"label": "Точка", "url": "https://example.com/tochka-bank"},
        ])

    def test_result_link_rejects_non_https_url(self):
        root = self.root / "invalid-result"
        self.seed(root, 0)
        self.cli(root, ["--json-min"], [
            "register", "--id", "ours", "--task-name", "task", "--project", "demo",
            "--owner", "agent", "--harness", "codex", "--hostname", "host", "--cwd", "/repo",
        ])
        error, _ = self.cli(root, ["--json-min"], [
            "status", "--plan", "ours", "--status", "completed",
            "--result-link", "site=http://example.com",
        ], 2)
        self.assertIn("HTTPS URL", error["error"])

    def test_heartbeat_revives_stale_plan(self):
        root = self.root / "heartbeat-revival"
        self.seed(root, 0)
        self.cli(root, ["--json-min"], [
            "register", "--id", "ours", "--task-name", "task", "--project", "demo",
            "--owner", "agent", "--harness", "codex", "--hostname", "host", "--cwd", "/repo",
        ])
        self.cli(root, ["--json-min"], ["status", "--plan", "ours", "--status", "stale"])
        self.cli(root, ["--json-min"], ["heartbeat", "--plan", "ours", "--lease-minutes", "45"])
        shown, _ = self.cli(root, ["--json-min"], ["show", "--plan", "ours"])
        self.assertEqual(shown["status"], "active")
        with closing(sqlite3.connect(root / "db.sqlite")) as conn:
            payloads = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM work_events WHERE plan_id='ours' AND event_type='status_changed' ORDER BY id"
            )]
        self.assertEqual(payloads[-1], {
            "from": "stale", "to": "active", "reason": "heartbeat_recovered",
        })

    def test_errors_and_snapshot_failures_remain_visible(self):
        root = self.root / "errors"
        self.seed(root, 0)
        before = self.state(root)
        for flags in (["--json"], ["--json-min", "--brief"]):
            error, _ = self.cli(root, flags, ["show", "--plan", "missing"], 2)
            self.assertFalse(error["ok"])
            self.assertIn("missing", error["error"])
            with mock.patch.object(memctl, "render_snapshots", side_effect=OSError("disk full")):
                error, _ = self.cli(root, flags, ["snapshot"], 2)
            self.assertEqual(error, {"ok": False, "error": "disk full"})
        self.assertEqual(self.state(root), before)


if __name__ == "__main__":
    unittest.main()
