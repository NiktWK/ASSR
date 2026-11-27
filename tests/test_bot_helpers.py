"""Tests for pure Telegram-bot helpers and shared DB helpers."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import team
import team_db
import tg_bot


class ModelIdTest(unittest.TestCase):
    def test_parse_provider_model(self) -> None:
        self.assertEqual(
            tg_bot.parse_model_id("anymodel/am/nemotron-3-ultra-550b-a55b"),
            {"providerID": "anymodel", "modelID": "am/nemotron-3-ultra-550b-a55b"},
        )

    def test_parse_invalid(self) -> None:
        self.assertIsNone(tg_bot.parse_model_id("nemotron"))
        self.assertIsNone(tg_bot.parse_model_id("/model"))
        self.assertIsNone(tg_bot.parse_model_id("provider/"))


class PromptBodyTest(unittest.TestCase):
    def test_model_is_forwarded_in_message_body(self) -> None:
        client = team.OpenCode("http://127.0.0.1:1", timeout=5)
        captured = {}

        def fake_request(method, path, body=None, timeout=None):
            captured["body"] = body
            return {"info": {}, "parts": [{"type": "text", "text": "ok"}]}

        client.request = fake_request
        client.messages = lambda session_id: []
        result = client.prompt(
            "sid",
            "secretary",
            "hi",
            attempts=1,
            stall_timeout=0,
            model={"providerID": "p", "modelID": "m"},
        )
        self.assertEqual(team.extract_text(result), "ok")
        self.assertEqual(captured["body"]["agent"], "secretary")
        self.assertEqual(captured["body"]["model"], {"providerID": "p", "modelID": "m"})


class SplitTextTest(unittest.TestCase):
    def test_short_text_single_chunk(self) -> None:
        self.assertEqual(tg_bot.split_text("привет"), ["привет"])

    def test_long_text_split_and_lossless(self) -> None:
        text = "\n".join(f"строка {i}" for i in range(2000))
        chunks = tg_bot.split_text(text, limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_empty_text(self) -> None:
        self.assertEqual(tg_bot.split_text(""), ["(пусто)"])


class EventLineTest(unittest.TestCase):
    def make(self, role: str, action: str, payload: dict) -> dict:
        return {"role": role, "action": action, "payload": json.dumps(payload)}

    def test_submit_has_no_content(self) -> None:
        event = self.make(
            "developer", "submit",
            {"phase": "build", "task_id": "t1", "content": "СЕКРЕТНЫЙ ТЕКСТ"},
        )
        line = tg_bot.event_line(event)
        self.assertIsNotNone(line)
        self.assertIn("отчёт", line)
        self.assertNotIn("СЕКРЕТНЫЙ ТЕКСТ", line)

    def test_review_verdict(self) -> None:
        line = tg_bot.event_line(
            self.make("researcher", "review", {"phase": "plan", "task_id": "t1", "verdict": "approve"})
        )
        self.assertIn("принято", line)

    def test_unknown_event_ignored(self) -> None:
        self.assertIsNone(
            tg_bot.event_line(self.make("researcher", "sources", {"url": "x"}))
        )


class KeyboardTest(unittest.TestCase):
    def test_approval_buttons(self) -> None:
        kb = tg_bot.question_keyboard({"id": 5, "kind": "accept_final"})
        data = [button.callback_data for row in kb.inline_keyboard for button in row]
        self.assertEqual(data, ["q:5:approve", "q:5:revise"])

    def test_agent_question_button(self) -> None:
        kb = tg_bot.question_keyboard({"id": 9, "kind": "agent"})
        data = [button.callback_data for row in kb.inline_keyboard for button in row]
        self.assertEqual(data, ["q:9:answer"])


class ConfigTest(unittest.TestCase):
    def test_env_priority(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "env-token", "TELEGRAM_ALLOWED_CHAT_ID": "42"},
        ):
            token, chat_id = tg_bot.load_config()
        self.assertEqual(token, "env-token")
        self.assertEqual(chat_id, 42)

    def test_file_fallback(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="botcfg-"))
        config = tmp / "telegram.json"
        config.write_text(json.dumps({"token": "file-token", "chat_id": 777}), "utf-8")
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_CHAT_ID": ""},
        ), mock.patch.object(tg_bot, "CONFIG_FILE", config):
            token, chat_id = tg_bot.load_config()
        self.assertEqual(token, "file-token")
        self.assertEqual(chat_id, 777)

    def test_missing_config(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="botcfg-"))
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_CHAT_ID": ""},
        ), mock.patch.object(tg_bot, "CONFIG_FILE", tmp / "nope.json"):
            token, chat_id = tg_bot.load_config()
        self.assertIsNone(token)
        self.assertIsNone(chat_id)


class AnswerHumanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="botdb-"))
        self.db = self.tmp / "state.db"
        team_db.init(self.db)
        self.orig = tg_bot.DB_PATH
        tg_bot.DB_PATH = self.db

    def tearDown(self) -> None:
        tg_bot.DB_PATH = self.orig

    def test_answer_human(self) -> None:
        conn = team_db.connect(self.db)
        qid = team_db.new_question(conn, "controller", "human", "agent", "Вопрос?")
        conn.close()
        ok, message = tg_bot.answer_human(qid, "approve")
        self.assertTrue(ok)
        self.assertIn("принят", message)
        conn = team_db.connect(self.db)
        try:
            question = team_db.get_question(conn, qid)
            self.assertEqual(question["status"], "answered")
            events = [r["action"] for r in team_db.recent_events(conn, 3)]
            self.assertIn("answer", events)
        finally:
            conn.close()

    def test_answer_closed_rejected(self) -> None:
        conn = team_db.connect(self.db)
        qid = team_db.new_question(conn, "controller", "human", "agent", "Вопрос?")
        team_db.answer_question(conn, qid, "done")
        conn.close()
        ok, message = tg_bot.answer_human(qid, "again")
        self.assertFalse(ok)
        self.assertIn("уже закрыт", message)

    def test_last_final_result(self) -> None:
        conn = team_db.connect(self.db)
        team_db.add_event(
            conn, "controller", "submit", {"phase": "build", "content": "ФИНАЛ"}
        )
        self.assertEqual(team_db.last_final_result(conn), "ФИНАЛ")
        conn.close()


if __name__ == "__main__":
    unittest.main()