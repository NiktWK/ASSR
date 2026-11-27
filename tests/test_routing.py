"""Routing and validation tests for the AutomateML team orchestrator.

Uses a fake opencode client: we feed events that the `team` tool would produce and
assert which prompt the orchestrator sends next.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import team
import team_agent
import team_db


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.models: list[tuple[str, object]] = []

    def prompt(self, session_id, agent, text, attempts=3, stall_timeout=300, model=None):
        self.calls.append((session_id, agent, text))
        self.models.append((agent, model))
        return {"parts": [{"type": "text", "text": f"fake:{agent}"}]}

    def abort(self, session_id):
        pass


class BaseTeamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="team-test-"))
        self.db = self.tmp / "state.db"
        team_db.init(self.db)
        conn = team_db.connect(self.db)
        self.sessions = {}
        for role in team_db.ROLES:
            sid = f"ses_{role}"
            self.sessions[role] = sid
            team_db.set_session(conn, role, sid)
        conn.close()
        self.fake = FakeClient()
        self.orch = team.Orchestrator(
            "тестовая задача",
            {"url": "http://127.0.0.1:1", "password": ""},
            self.sessions,
            timeout=5,
            db_path=self.db,
            run_dir=self.tmp,
        )
        self.orch.client = self.fake

    def tearDown(self) -> None:
        self.orch.conn.close()

    # -- helpers ---------------------------------------------------------
    def feed(self, role: str, action: str, payload: dict) -> None:
        conn = team_db.connect(self.db)
        try:
            team_db.add_event(conn, role, action, payload)
            event = team_db.next_event(conn)
            with contextlib.redirect_stdout(io.StringIO()):
                self.orch.handle_event(event)
            team_db.consume_event(conn, event["id"])
        finally:
            conn.close()

    def add_event(self, role: str, action: str, payload: dict) -> int:
        conn = team_db.connect(self.db)
        try:
            return team_db.add_event(conn, role, action, payload)
        finally:
            conn.close()

    def handle_event_id(self, event_id: int) -> None:
        conn = team_db.connect(self.db)
        try:
            event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            with contextlib.redirect_stdout(io.StringIO()):
                self.orch.handle_event(event)
            team_db.consume_event(conn, event_id)
        finally:
            conn.close()

    def answer_human(self, qid: int, text: str) -> None:
        conn = team_db.connect(self.db)
        try:
            team_db.answer_question(conn, qid, text)
            team_db.add_event(
                conn, "human", "answer", {"question_id": qid, "text": text, "from_role": "human"}
            )
            event = team_db.next_event(conn)
            with contextlib.redirect_stdout(io.StringIO()):
                self.orch.handle_event(event)
            team_db.consume_event(conn, event["id"])
        finally:
            conn.close()

    def last_agent(self) -> str:
        self.assertTrue(self.fake.calls, "orchestrator did not prompt anyone")
        return self.fake.calls[-1][1]

    def last_text(self) -> str:
        return self.fake.calls[-1][2]

    def open_human_questions(self):
        conn = team_db.connect(self.db)
        try:
            return team_db.open_questions(conn, "human")
        finally:
            conn.close()

    def task(self, task_id: str):
        conn = team_db.connect(self.db)
        try:
            return team_db.get_task(conn, task_id)
        finally:
            conn.close()

    def set_task_status(self, task_id: str, status: str) -> None:
        conn = team_db.connect(self.db)
        try:
            team_db.set_task_status(conn, task_id, status)
        finally:
            conn.close()

    def make_task(self, worker: str, goal: str, status: str) -> str:
        conn = team_db.connect(self.db)
        try:
            task_id = team_db.new_task(conn, worker, goal, "архитектура")
            team_db.set_task_status(conn, task_id, status)
            return task_id
        finally:
            conn.close()


class FullFlowTest(BaseTeamTest):
    def test_happy_path(self) -> None:
        self.feed("controller", "submit", {"phase": "plan", "content": "директива", "task_id": None})
        self.assertEqual(len(self.fake.calls), 0)
        questions = self.open_human_questions()
        self.assertEqual(len(questions), 1)
        qid = questions[0]["id"]
        self.assertEqual(questions[0]["kind"], "approve_controller_plan")

        self.answer_human(qid, "approve")
        self.assertEqual(self.last_agent(), "researcher-plan")
        self.assertIn("директива", self.last_text())

        self.feed("researcher", "submit", {"phase": "plan", "content": "план", "task_id": None})
        self.assertEqual(self.last_agent(), "controller-build")
        self.assertIn("team review", self.last_text())

        self.feed("controller", "review", {"phase": "plan", "verdict": "approve", "comments": "", "task_id": None})
        self.assertEqual(self.last_agent(), "researcher-build")

        task_id = self.make_task("developer", "написать код", "plan_pending")
        self.assertEqual(task_id, "t1")
        self.feed("researcher", "assign", {"task_id": task_id, "worker": "developer", "goal": "написать код", "context": "арх"})
        self.assertEqual(self.last_agent(), "developer-plan")
        self.assertEqual(self.task(task_id)["worker"], "developer")

        self.set_task_status(task_id, "plan_review")
        self.feed("developer", "submit", {"phase": "plan", "content": "план dev", "task_id": "t1"})
        self.assertEqual(self.last_agent(), "researcher-build")
        self.assertIn('task_id="t1"', self.last_text())

        self.feed("researcher", "review", {"phase": "plan", "verdict": "approve", "comments": "ок", "task_id": "t1"})
        self.assertEqual(self.last_agent(), "developer-build")
        self.assertEqual(self.task("t1")["status"], "build_pending")

        self.set_task_status("t1", "build_review")
        self.feed("developer", "submit", {"phase": "build", "content": "готово", "task_id": "t1"})
        self.assertEqual(self.last_agent(), "researcher-build")

        self.feed("researcher", "review", {"phase": "build", "verdict": "approve", "comments": "", "task_id": "t1"})
        self.assertEqual(self.task("t1")["status"], "done")
        self.assertEqual(self.last_agent(), "researcher-build")

        self.feed("researcher", "submit", {"phase": "build", "content": "финальный отчёт", "task_id": None})
        self.assertEqual(self.last_agent(), "controller-build")

        self.feed("controller", "review", {"phase": "build", "verdict": "approve", "comments": "", "task_id": None})
        self.assertEqual(self.last_agent(), "controller-build")

        self.feed("controller", "submit", {"phase": "build", "content": "итог", "task_id": None})
        questions = self.open_human_questions()
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["kind"], "accept_final")

        self.answer_human(questions[0]["id"], "approve")
        conn = team_db.connect(self.db)
        try:
            self.assertEqual(team_db.get_meta(conn, "run_status"), "done")
        finally:
            conn.close()

    def test_send_passes_configured_model(self) -> None:
        model = {"providerID": "anymodel", "modelID": "glm-5.3-flash"}
        with mock.patch.object(team, "agent_profile_model", return_value=model):
            self.orch.send("developer", "build", "тестовое сообщение")
        self.assertEqual(self.fake.models[-1], ("developer-build", model))

    def test_inject_reaches_controller(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "controller", "build")
        conn.close()
        self.feed("human", "inject", {"text": "добавь проверку X", "target": "controller"})
        self.assertEqual(self.last_agent(), "controller-build")
        self.assertIn("СООБЩЕНИЕ ОТ ЧЕЛОВЕКА", self.last_text())
        self.assertIn("добавь проверку X", self.last_text())

    def test_revise_paths(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_meta(conn, "controller_plan", "директива")
        conn.close()

        self.feed("controller", "review", {"phase": "plan", "verdict": "revise", "comments": "мало критериев", "task_id": None})
        self.assertEqual(self.last_agent(), "researcher-plan")
        self.assertIn("мало критериев", self.last_text())

        task_id = self.make_task("developer", "задача", "plan_review")
        self.feed("researcher", "review", {"phase": "plan", "verdict": "revise", "comments": "добавь тесты", "task_id": task_id})
        self.assertEqual(self.last_agent(), "developer-plan")
        self.assertEqual(self.task(task_id)["status"], "plan_pending")
        self.assertIn("добавь тесты", self.last_text())

    def test_duplicate_submit_is_ignored(self) -> None:
        self.feed("controller", "submit", {"phase": "plan", "content": "директива", "task_id": None})
        self.assertEqual(len(self.open_human_questions()), 1)
        self.feed("controller", "submit", {"phase": "plan", "content": "директива-дубль", "task_id": None})
        self.assertEqual(len(self.open_human_questions()), 1)
        self.assertEqual(len(self.fake.calls), 0)

    def test_next_step_prompt_skipped_when_already_submitted(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "controller", "build")
        team_db.mark_routed(conn, "controller", "build")
        conn.close()
        self.feed("controller", "review", {"phase": "build", "verdict": "approve", "comments": "", "task_id": None})
        self.assertEqual(len(self.fake.calls), 0)

        task_id = self.make_task("tester", "проверка", "build_review")
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "researcher", "build")
        team_db.mark_routed(conn, "researcher", "build")
        conn.close()
        self.feed("researcher", "review", {"phase": "build", "verdict": "approve", "comments": "", "task_id": task_id})
        self.assertEqual(len(self.fake.calls), 0)

    def test_same_turn_submit_prevents_duplicate_prompt(self) -> None:
        task_id = self.make_task("tester", "проверка", "build_review")
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "researcher", "build")
        conn.close()
        review_id = self.add_event(
            "researcher", "review",
            {"phase": "build", "verdict": "approve", "comments": "", "task_id": task_id},
        )
        self.add_event("researcher", "submit", {"phase": "build", "content": "финал", "task_id": None})
        self.handle_event_id(review_id)
        self.assertEqual(len(self.fake.calls), 0)
        self.assertEqual(self.task(task_id)["status"], "done")

    def test_questions_route_up_the_hierarchy(self) -> None:
        conn = team_db.connect(self.db)
        qid = team_db.new_question(conn, "developer", "researcher", "agent", "как делать?", phase="plan")
        team_db.set_phase(conn, "researcher", "build")
        conn.close()
        self.feed("developer", "ask", {"question_id": qid, "to_role": "researcher"})
        self.assertEqual(self.last_agent(), "researcher-build")
        self.assertIn(f"q{qid}", self.last_text())

        conn = team_db.connect(self.db)
        q2 = team_db.new_question(conn, "researcher", "controller", "agent", "нужно решение", phase="build")
        team_db.set_phase(conn, "controller", "build")
        conn.close()
        self.feed("researcher", "ask", {"question_id": q2, "to_role": "controller"})
        self.assertEqual(self.last_agent(), "controller-build")


class ToolValidationTest(BaseTeamTest):
    def run_tool(self, args: list[str]) -> int:
        old = os.environ.get("TEAM_DB")
        os.environ["TEAM_DB"] = str(self.db)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return team_agent.run(args)
        finally:
            if old is None:
                os.environ.pop("TEAM_DB", None)
            else:
                os.environ["TEAM_DB"] = old

    def test_unknown_session_rejected(self) -> None:
        self.assertNotEqual(
            self.run_tool(["--session", "ses_unknown", "--agent", "developer-plan", "submit",
                           "--phase", "plan", "--content", "x"]),
            0,
        )

    def test_wrong_role_agent_rejected(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "developer", "plan")
        conn.close()
        self.assertNotEqual(
            self.run_tool(["--session", "ses_developer", "--agent", "researcher-plan", "submit",
                           "--phase", "plan", "--content", "x"]),
            0,
        )

    def test_assign_only_researcher_build(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "developer", "build")
        conn.close()
        self.assertNotEqual(
            self.run_tool(["--session", "ses_developer", "--agent", "developer-build", "assign",
                           "--worker", "developer", "--goal", "x"]),
            0,
        )

    def test_assign_creates_task_and_blocks_second_active(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "researcher", "build")
        conn.close()
        self.assertEqual(
            self.run_tool(["--session", "ses_researcher", "--agent", "researcher-build", "assign",
                           "--worker", "developer", "--goal", "сделать", "--context", "контекст"]),
            0,
        )
        self.assertNotEqual(
            self.run_tool(["--session", "ses_researcher", "--agent", "researcher-build", "assign",
                           "--worker", "developer", "--goal", "ещё"]),
            0,
        )

    def test_ask_answer_roundtrip(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "developer", "plan")
        conn.close()
        self.assertEqual(
            self.run_tool(["--session", "ses_developer", "--agent", "developer-plan", "ask",
                           "--question", "что именно делать?"]),
            0,
        )
        conn = team_db.connect(self.db)
        try:
            q = team_db.open_questions(conn, "researcher")[0]
            self.assertEqual(q["from_role"], "developer")
            team_db.set_phase(conn, "researcher", "build")
        finally:
            conn.close()
        self.assertEqual(
            self.run_tool(["--session", "ses_researcher", "--agent", "researcher-build", "answer",
                           "--question-id", f"q{q['id']}", "--answer", "делай вот так"]),
            0,
        )
        conn = team_db.connect(self.db)
        try:
            done = team_db.get_question(conn, q["id"])
            self.assertEqual(done["status"], "answered")
            self.assertEqual(done["answer"], "делай вот так")
        finally:
            conn.close()

    def test_ensure_phase_keeps_flags_within_phase(self) -> None:
        conn = team_db.connect(self.db)
        try:
            team_db.set_phase(conn, "developer", "plan")
            team_db.mark_submitted(conn, "developer")
            team_db.mark_routed(conn, "developer", "plan")
            team_db.ensure_phase(conn, "developer", "plan")
            self.assertTrue(team_db.is_submitted(conn, "developer"))
            self.assertTrue(team_db.is_routed(conn, "developer", "plan"))
            team_db.ensure_phase(conn, "developer", "build")
            self.assertFalse(team_db.is_submitted(conn, "developer"))
            self.assertFalse(team_db.is_routed(conn, "developer", "build"))
            team_db.set_phase(conn, "developer", "plan")
            self.assertFalse(team_db.is_submitted(conn, "developer"))
        finally:
            conn.close()

    def test_revise_restarts_phase(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "researcher", "build")
        team_db.mark_submitted(conn, "researcher")
        team_db.mark_routed(conn, "researcher", "build")
        team_db.set_meta(conn, "controller_plan", "директива")
        conn.close()

        self.feed("controller", "review", {"phase": "build", "verdict": "revise", "comments": "переделать", "task_id": None})
        self.assertEqual(self.last_agent(), "researcher-build")
        conn = team_db.connect(self.db)
        try:
            self.assertFalse(team_db.is_submitted(conn, "researcher"))
            self.assertFalse(team_db.is_routed(conn, "researcher", "build"))
        finally:
            conn.close()

    def test_submit_requires_matching_phase(self) -> None:
        conn = team_db.connect(self.db)
        team_db.set_phase(conn, "researcher", "plan")
        conn.close()
        self.assertNotEqual(
            self.run_tool(["--session", "ses_researcher", "--agent", "researcher-build", "submit",
                           "--phase", "build", "--content", "x"]),
            0,
        )


if __name__ == "__main__":
    unittest.main()