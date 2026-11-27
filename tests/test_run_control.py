"""Tests for run_control: active-run detection, start lock, cancellation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run_control


class FakeProc:
    returncode = None
    pid = 4242

    def poll(self):
        return None


class RunControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="runctl-"))
        self.orig = (
            run_control.RUN_DIR,
            run_control.PID_FILE,
            run_control.CANCEL_FILE,
            run_control.LOCK_FILE,
            run_control.CONSOLE_FILE,
        )
        run_control.RUN_DIR = self.tmp
        run_control.PID_FILE = self.tmp / "orchestrator.pid"
        run_control.CANCEL_FILE = self.tmp / "cancel"
        run_control.LOCK_FILE = self.tmp / "start.lock"
        run_control.CONSOLE_FILE = self.tmp / "console.log"

    def tearDown(self) -> None:
        (
            run_control.RUN_DIR,
            run_control.PID_FILE,
            run_control.CANCEL_FILE,
            run_control.LOCK_FILE,
            run_control.CONSOLE_FILE,
        ) = self.orig

    def test_not_active_without_pid_file(self) -> None:
        self.assertFalse(run_control.is_active())

    def test_not_active_for_dead_pid(self) -> None:
        run_control.PID_FILE.write_text("99999999")
        self.assertFalse(run_control.is_active())

    def test_empty_task_rejected(self) -> None:
        with self.assertRaises(run_control.RunError):
            run_control.start_run("   ")

    def test_start_refused_when_active(self) -> None:
        with mock.patch.object(run_control, "is_active", return_value=True):
            with self.assertRaises(run_control.RunBusy):
                run_control.start_run("задача")

    def test_start_spawns_orchestrator(self) -> None:
        with mock.patch.object(
            run_control, "is_active", side_effect=[False, True]
        ), mock.patch.object(run_control.subprocess, "Popen", return_value=FakeProc()):
            result = run_control.start_run("задача")
        self.assertEqual(result["pid"], 4242)
        self.assertTrue(run_control.CONSOLE_FILE.exists())

    def test_cancel_without_active_run(self) -> None:
        with mock.patch.object(run_control, "is_active", return_value=False):
            self.assertFalse(run_control.cancel_run())
        self.assertFalse(run_control.CANCEL_FILE.exists())

    def test_cancel_active_run(self) -> None:
        run_control.PID_FILE.write_text("4242")
        with mock.patch.object(
            run_control, "is_active", side_effect=[True, False]
        ), mock.patch.object(run_control, "_abort_all_sessions"), mock.patch.object(
            run_control, "_mark_cancelled"
        ) as mark:
            self.assertTrue(run_control.cancel_run())
        self.assertTrue(run_control.CANCEL_FILE.exists())
        mark.assert_called_once()


if __name__ == "__main__":
    unittest.main()