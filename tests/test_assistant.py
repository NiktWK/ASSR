"""Tests for the secretary tool backend (assistant_agent.py)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import assistant_agent
import model_admin
import run_control
import team_db


class AssistantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="assistant-"))
        self.db = self.tmp / "state.db"
        self.orig = (assistant_agent.RUN_DIR, assistant_agent.DB_PATH, assistant_agent.SECRETARY_FILE)
        assistant_agent.RUN_DIR = self.tmp
        assistant_agent.DB_PATH = self.db
        assistant_agent.SECRETARY_FILE = self.tmp / "secretary.json"
        assistant_agent.SECRETARY_FILE.write_text(
            json.dumps({"session_id": "ses_secretary"}), "utf-8"
        )
        team_db.init(self.db)
        conn = team_db.connect(self.db)
        for role in team_db.ROLES:
            team_db.set_session(conn, role, f"ses_{role}")
        team_db.set_meta(conn, "run_status", "running")
        team_db.set_meta(conn, "run_tag", "20260101-000000")
        team_db.set_meta(conn, "task", "тестовая задача")
        team_db.set_phase(conn, "researcher", "build")
        conn.close()
        self.old_env = os.environ.get("TEAM_DB")
        os.environ["TEAM_DB"] = str(self.db)

    def tearDown(self) -> None:
        (
            assistant_agent.RUN_DIR,
            assistant_agent.DB_PATH,
            assistant_agent.SECRETARY_FILE,
        ) = self.orig
        if self.old_env is None:
            os.environ.pop("TEAM_DB", None)
        else:
            os.environ["TEAM_DB"] = self.old_env

    def run_tool(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = assistant_agent.run(args)
        return rc, out.getvalue(), err.getvalue()

    def call(self, action: str, *extra: str) -> tuple[int, str, str]:
        return self.run_tool(["--session", "ses_secretary", action, *extra])

    def test_rejects_foreign_session(self) -> None:
        rc, _, err = self.run_tool(["--session", "ses_developer", "status"])
        self.assertEqual(rc, 2)
        self.assertIn("только сессии секретаря", err)

    def test_status(self) -> None:
        rc, out, _ = self.call("status")
        self.assertEqual(rc, 0)
        self.assertIn("run_status=running", out)
        self.assertIn("тестовая задача", out)

    def test_tasks_and_events(self) -> None:
        conn = team_db.connect(self.db)
        team_db.new_task(conn, "developer", "сделать что-то")
        team_db.add_event(
            conn, "developer", "submit",
            {"phase": "plan", "content": "СЕКРЕТНЫЙ ТЕКСТ", "task_id": "t1"},
        )
        conn.close()
        rc, out, _ = self.call("tasks")
        self.assertEqual(rc, 0)
        self.assertIn("t1", out)
        rc, out, _ = self.call("events")
        self.assertEqual(rc, 0)
        self.assertNotIn("СЕКРЕТНЫЙ ТЕКСТ", out)

    def test_questions_and_answer(self) -> None:
        conn = team_db.connect(self.db)
        qid = team_db.new_question(
            conn, "controller", "human", "agent", "Что делать дальше?"
        )
        conn.close()
        rc, out, _ = self.call("questions")
        self.assertEqual(rc, 0)
        self.assertIn("Что делать дальше?", out)
        rc, out, _ = self.call("question", "--id", f"q{qid}")
        self.assertEqual(rc, 0)
        self.assertIn("Что делать дальше?", out)
        rc, out, _ = self.call("answer", "--id", f"q{qid}", "--text", "approve")
        self.assertEqual(rc, 0)
        conn = team_db.connect(self.db)
        try:
            question = team_db.get_question(conn, qid)
            self.assertEqual(question["status"], "answered")
            self.assertEqual(question["answer"], "approve")
            events = [r["action"] for r in team_db.recent_events(conn, 5)]
            self.assertIn("answer", events)
        finally:
            conn.close()

    def test_answer_closed_question_rejected(self) -> None:
        conn = team_db.connect(self.db)
        qid = team_db.new_question(conn, "controller", "human", "agent", "x")
        team_db.answer_question(conn, qid, "done")
        conn.close()
        rc, _, err = self.call("answer", "--id", f"q{qid}", "--text", "again")
        self.assertEqual(rc, 2)
        self.assertIn("уже закрыт", err)

    def test_start_ok_and_busy(self) -> None:
        with mock.patch.object(
            run_control, "start_run", return_value={"pid": 7, "run_tag": "tag"}
        ) as start:
            rc, out, _ = self.call("start", "--task", "новая задача")
        self.assertEqual(rc, 0)
        self.assertIn("запущен", out)
        start.assert_called_once_with("новая задача")

        with mock.patch.object(run_control, "start_run", side_effect=run_control.RunBusy):
            rc, out, _ = self.call("start", "--task", "ещё")
        self.assertEqual(rc, 0)
        self.assertIn("уже выполняется", out)

    def test_stop(self) -> None:
        with mock.patch.object(run_control, "is_active", return_value=True), mock.patch.object(
            run_control, "cancel_run"
        ) as cancel:
            rc, out, _ = self.call("stop")
        self.assertEqual(rc, 0)
        self.assertIn("Отмена", out)
        cancel.assert_called_once()

    def test_tell_controller_requires_active_run(self) -> None:
        with mock.patch.object(run_control, "is_active", return_value=False):
            rc, _, err = self.call("tell_controller", "--text", "добавь X")
        self.assertEqual(rc, 2)
        self.assertIn("нет активного прогона", err)

        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "controller", "build")
        conn.close()
        with mock.patch.object(run_control, "is_active", return_value=True):
            rc, out, _ = self.call("tell_controller", "--text", "добавь X")
        self.assertEqual(rc, 0)
        self.assertIn("будет доставлено", out)
        conn = team_db.connect(self.db)
        try:
            row = conn.execute(
                "SELECT * FROM events WHERE action='inject' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            payload = json.loads(row["payload"])
            self.assertEqual(payload["text"], "добавь X")
            self.assertEqual(payload["target"], "controller")
        finally:
            conn.close()

    def test_tell_controller_during_plan_is_soft_refused(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "controller", "plan")
        conn.close()
        with mock.patch.object(run_control, "is_active", return_value=True):
            rc, out, _ = self.call("tell_controller", "--text", "добавь X")
        self.assertEqual(rc, 0)
        self.assertIn("согласование плана", out)
        conn = team_db.connect(self.db)
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE action='inject'").fetchone()
            self.assertEqual(row["n"], 0)
        finally:
            conn.close()

    def test_models(self) -> None:
        with mock.patch.object(model_admin, "models_text", return_value="MODELS OK"):
            rc, out, _ = self.call("models")
        self.assertEqual(rc, 0)
        self.assertIn("MODELS OK", out)

    def test_setmodel_ok(self) -> None:
        with mock.patch.object(model_admin, "live_check", return_value=(True, "anymodel/x")), \
             mock.patch.object(model_admin, "write_config") as write, \
             mock.patch.object(model_admin, "mark_pending") as pending, \
             mock.patch.object(run_control, "is_active", return_value=True):
            rc, out, _ = self.call("setmodel", "--target", "tester-plan", "--model", "anymodel/x")
        self.assertEqual(rc, 0)
        self.assertIn("записана", out)
        self.assertIn("безопасном окне", out)
        write.assert_called_once_with(["tester-plan"], "anymodel/x")
        pending.assert_called_once_with(["tester-plan"], "anymodel/x")

    def test_setmodel_bad_target_and_model(self) -> None:
        rc, _, err = self.call("setmodel", "--target", "nope", "--model", "anymodel/x")
        self.assertEqual(rc, 2)
        self.assertIn("неизвестная цель", err)
        rc, _, err = self.call("setmodel", "--target", "tester-plan", "--model", "bad")
        self.assertEqual(rc, 2)
        self.assertIn("provider/model", err)

    def test_setmodel_rejected_by_live_check(self) -> None:
        with mock.patch.object(model_admin, "live_check", return_value=(False, "нет модели")), \
             mock.patch.object(model_admin, "write_config") as write:
            rc, _, err = self.call("setmodel", "--target", "tester-plan", "--model", "anymodel/x")
        self.assertEqual(rc, 2)
        self.assertIn("не принята", err)
        write.assert_not_called()

    def test_result(self) -> None:
        conn = team_db.connect(self.db)
        team_db.add_event(
            conn, "controller", "submit",
            {"phase": "build", "content": "ИТОГОВОЕ ЗАКЛЮЧЕНИЕ", "task_id": None},
        )
        conn.close()
        rc, out, _ = self.call("result")
        self.assertEqual(rc, 0)
        self.assertIn("ИТОГОВОЕ ЗАКЛЮЧЕНИЕ", out)


if __name__ == "__main__":
    unittest.main()