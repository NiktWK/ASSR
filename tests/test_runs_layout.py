"""Tests for the per-run directory layout, resume manifest and DB backups."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import team
import team_db


class ResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="layout-"))
        self.orig = (team_db.RUN_ROOT, team_db.POINTER_FILE)
        self.env = {k: os.environ.get(k) for k in ("TEAM_RUN_DIR", "TEAM_DB")}
        for k in self.env:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        team_db.RUN_ROOT, team_db.POINTER_FILE = self.orig
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_run_dir_wins(self) -> None:
        os.environ["TEAM_RUN_DIR"] = str(self.tmp / "env")
        self.assertEqual(team_db.current_run_dir(), (self.tmp / "env").resolve())

    def test_pointer_is_used(self) -> None:
        run = self.tmp / "runs" / "20260101-000000"
        run.mkdir(parents=True)
        (run / "state.db").write_text("", "utf-8")
        pointer = self.tmp / "current.json"
        pointer.write_text(json.dumps({"run_tag": run.name, "dir": f"runs/{run.name}"}), "utf-8")
        team_db.RUN_ROOT = self.tmp
        team_db.POINTER_FILE = pointer
        self.assertEqual(team_db.current_run_dir(), run)

    def test_pointer_ignored_without_db(self) -> None:
        pointer = self.tmp / "current.json"
        pointer.write_text(json.dumps({"dir": "runs/missing"}), "utf-8")
        team_db.RUN_ROOT = self.tmp
        team_db.POINTER_FILE = pointer
        self.assertEqual(team_db.current_run_dir(), self.tmp)

    def test_legacy_fallback(self) -> None:
        (self.tmp / "state.db").write_text("", "utf-8")
        team_db.RUN_ROOT = self.tmp
        team_db.POINTER_FILE = self.tmp / "current.json"
        self.assertEqual(team_db.current_run_dir(), self.tmp)

    def test_db_path_env_wins(self) -> None:
        os.environ["TEAM_DB"] = str(self.tmp / "x.db")
        self.assertEqual(team_db.db_path(), (self.tmp / "x.db").resolve())

    def test_db_path_from_pointer(self) -> None:
        run = self.tmp / "runs" / "tag"
        run.mkdir(parents=True)
        (run / "state.db").write_text("", "utf-8")
        pointer = self.tmp / "current.json"
        pointer.write_text(json.dumps({"dir": "runs/tag"}), "utf-8")
        team_db.RUN_ROOT = self.tmp
        team_db.POINTER_FILE = pointer
        self.assertEqual(team_db.db_path(), run / "state.db")


class RunDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rundir-"))
        self.orig = (team.RUN_DIR, team.RUNS_DIR, team.POINTER_FILE, team.DB_PATH,
                     team.LOG_FILE, team.CHAT_DIR)
        team.RUN_DIR = self.tmp
        team.RUNS_DIR = self.tmp / "runs"
        team.POINTER_FILE = self.tmp / "current.json"
        team.DB_PATH = self.tmp / "state.db"
        team.LOG_FILE = self.tmp / "events.log"
        team.CHAT_DIR = self.tmp / "chat"

    def tearDown(self) -> None:
        (team.RUN_DIR, team.RUNS_DIR, team.POINTER_FILE, team.DB_PATH,
         team.LOG_FILE, team.CHAT_DIR) = self.orig

    def test_new_run_dir_keeps_other_runs(self) -> None:
        first = team.new_run_dir("20260101-000000")
        team_db.connect(first / "state.db").close()
        second = team.new_run_dir("20260101-000001")
        self.assertTrue((first / "state.db").exists())
        self.assertTrue((second / "state.db").exists())
        self.assertTrue((second / "chat").is_dir())

    def test_write_pointer_and_manifest(self) -> None:
        run = team.new_run_dir("tag1")
        team.write_pointer("tag1", run)
        team.write_run_manifest(run, "tag1", "задача", {"developer": "ses_x"})
        pointer = json.loads(team.POINTER_FILE.read_text("utf-8"))
        self.assertEqual(pointer["run_tag"], "tag1")
        manifest = team.load_run_manifest(run)
        self.assertEqual(manifest["task"], "задача")
        self.assertEqual(manifest["sessions"], {"developer": "ses_x"})

    def test_migrate_legacy_run(self) -> None:
        team_db.init(self.tmp / "state.db")
        conn = team_db.connect(self.tmp / "state.db")
        try:
            team_db.set_meta(conn, "run_tag", "20260101-120000")
        finally:
            conn.close()
        team.CHAT_DIR.mkdir(parents=True, exist_ok=True)
        (team.CHAT_DIR / "developer.md").write_text("чат", "utf-8")
        team.LOG_FILE.write_text("лог", "utf-8")

        tag = team.migrate_legacy_run()
        self.assertEqual(tag, "20260101-120000")
        dest = self.tmp / "runs" / tag
        self.assertTrue((dest / "state.db").exists())
        self.assertTrue((dest / "chat" / "developer.md").exists())
        self.assertTrue((dest / "events.log").exists())
        self.assertFalse((self.tmp / "state.db").exists())
        self.assertFalse(team.CHAT_DIR.exists())
        pointer = json.loads(team.POINTER_FILE.read_text("utf-8"))
        self.assertEqual(pointer["dir"], f"runs/{tag}")

    def test_migrate_noop_when_nothing(self) -> None:
        self.assertIsNone(team.migrate_legacy_run())

    def test_backup_state_rotates(self) -> None:
        run = team.new_run_dir("bk")
        conn = team_db.connect(run / "state.db")
        try:
            team_db.set_meta(conn, "task", "x")
            with mock.patch.object(team.time, "time", side_effect=[1.0, 2.0, 3.0]):
                team.backup_state(conn, run, keep=2)
                team.backup_state(conn, run, keep=2)
                team.backup_state(conn, run, keep=2)
        finally:
            conn.close()
        files = sorted((run / "backups").glob("state-*.db"))
        self.assertEqual(len(files), 2)
        self.assertTrue(files[-1].exists())

    def test_saved_run_and_blocker(self) -> None:
        self.assertFalse(team.has_saved_run())
        team.POINTER_FILE.write_text("{}", "utf-8")
        self.assertTrue(team.has_saved_run())
        with mock.patch("run_control.is_active", return_value=False):
            self.assertIsNone(team.new_run_blocker(fresh=True))
            self.assertIsNotNone(team.new_run_blocker(fresh=False))
        with mock.patch("run_control.is_active", return_value=True):
            self.assertIn("выполняется", team.new_run_blocker(fresh=True))


if __name__ == "__main__":
    unittest.main()
