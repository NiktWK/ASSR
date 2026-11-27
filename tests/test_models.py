"""Tests for model resolution and server restart helpers in team.py."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import team
import team_db


class ModelResolutionTest(unittest.TestCase):
    def test_parse_model_id(self) -> None:
        self.assertEqual(
            team.parse_model_id("anymodel/am/nemotron-3-ultra-550b-a55b"),
            {"providerID": "anymodel", "modelID": "am/nemotron-3-ultra-550b-a55b"},
        )
        self.assertIsNone(team.parse_model_id("nemotron"))
        self.assertIsNone(team.parse_model_id("/x"))
        self.assertIsNone(team.parse_model_id("p/"))
        self.assertIsNone(team.parse_model_id(None))

    def test_reads_and_reloads_on_change(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="models-"))
        cfg = tmp / "opencode.json"
        cfg.write_text(json.dumps({"agent": {"developer-build": {"model": "anymodel/glm-5.3"}}}))
        with mock.patch.object(team, "PROJECT", tmp):
            team.MODEL_CACHE["mtime"] = None
            self.assertEqual(
                team.agent_profile_model("developer-build"),
                {"providerID": "anymodel", "modelID": "glm-5.3"},
            )
            cfg.write_text(json.dumps({"agent": {"developer-build": {"model": "anymodel/fresh"}}}))
            os.utime(cfg, (time.time() + 5, time.time() + 5))
            self.assertEqual(
                team.agent_profile_model("developer-build"),
                {"providerID": "anymodel", "modelID": "fresh"},
            )
            self.assertIsNone(team.agent_profile_model("unknown-profile"))
        team.MODEL_CACHE["mtime"] = None

    def test_agent_config_models_missing_file(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="models-"))
        with mock.patch.object(team, "PROJECT", tmp):
            team.MODEL_CACHE["mtime"] = None
            self.assertEqual(team.agent_config_models(), {})
        team.MODEL_CACHE["mtime"] = None


class ToolsMtimeTest(unittest.TestCase):
    def test_tools_mtime(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="tools-"))
        (tmp / ".opencode" / "tools").mkdir(parents=True)
        (tmp / ".opencode" / "tools" / "x.ts").write_text("// tool")
        (tmp / "prompts").mkdir()
        (tmp / "prompts" / "x.md").write_text("# prompt")
        with mock.patch.object(team, "PROJECT", tmp):
            self.assertGreater(team.tools_mtime(), 0)


class RequestErrorTest(unittest.TestCase):
    def test_timeout_becomes_retryable_opencode_error(self) -> None:
        client = team.OpenCode("http://127.0.0.1:1", timeout=5)
        with mock.patch.object(team, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(team.OpenCodeError) as ctx:
                client.request("GET", "/config", timeout=1)
        self.assertTrue(ctx.exception.retryable)
        self.assertIsNone(ctx.exception.status)

    def test_urlerror_is_retryable(self) -> None:
        client = team.OpenCode("http://127.0.0.1:1", timeout=5)
        with mock.patch.object(team, "urlopen", side_effect=URLError("connection refused")):
            with self.assertRaises(team.OpenCodeError) as ctx:
                client.request("GET", "/config", timeout=1)
        self.assertTrue(ctx.exception.retryable)


class PromptRetryTest(unittest.TestCase):
    def test_retries_on_retryable_network_error(self) -> None:
        client = team.OpenCode("http://127.0.0.1:1", timeout=5)
        calls = {"n": 0}

        def fake_request(method, path, body=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise team.OpenCodeError("сеть: timed out", retryable=True)
            return {"info": {}, "parts": [{"type": "text", "text": "ok"}]}

        client.request = fake_request
        client.messages = lambda session_id: []
        with mock.patch.object(team.time, "sleep"):
            result = client.prompt("sid", "developer-build", "hi", attempts=3, stall_timeout=0)
        self.assertEqual(team.extract_text(result), "ok")
        self.assertEqual(calls["n"], 2)


class LoadSessionsTest(unittest.TestCase):
    def _db(self):
        tmp = Path(tempfile.mkdtemp(prefix="loadsess-"))
        db = tmp / "state.db"
        team_db.init(db)
        return db

    def test_loads_all_roles(self) -> None:
        db = self._db()
        conn = team_db.connect(db)
        try:
            for role in team.ROLES:
                team_db.set_session(conn, role, f"ses_{role}")
            sessions = team.load_sessions(conn)
        finally:
            conn.close()
        self.assertEqual(set(sessions), set(team.ROLES))
        self.assertEqual(sessions["developer"], "ses_developer")

    def test_missing_role_raises(self) -> None:
        db = self._db()
        conn = team_db.connect(db)
        try:
            team_db.set_session(conn, "controller", "x")
            with self.assertRaises(SystemExit):
                team.load_sessions(conn)
        finally:
            conn.close()


class RestartServerTest(unittest.TestCase):
    def test_keep_auth_no_saved_server_uses_ensure(self) -> None:
        with mock.patch.object(team, "load_server", return_value=None), mock.patch.object(
            team, "ensure_server", return_value={"url": "http://x"}
        ):
            ok, message = team.restart_server_keep_auth(verbose=False)
        self.assertTrue(ok)
        self.assertIn("http://x", message)

    def test_keep_auth_reuses_port_and_password(self) -> None:
        saved = {"url": "http://127.0.0.1:4097", "port": 4097, "password": "secret", "pid": 111}
        captured = {}

        def fake_start(port, password, verbose, base=None):
            captured["port"] = port
            captured["password"] = password
            return {"url": saved["url"]}

        with mock.patch.object(team, "load_server", return_value=saved), mock.patch.object(
            team, "_stop_and_wait"
        ), mock.patch.object(team, "_wait_port_free", return_value=True), mock.patch.object(
            team, "_start_server", side_effect=fake_start
        ):
            ok, message = team.restart_server_keep_auth(verbose=False)
        self.assertTrue(ok)
        self.assertIn("перезапущен", message)
        self.assertEqual(captured["port"], 4097)
        self.assertEqual(captured["password"], "secret")

    def test_ensure_server_reuses_auth_when_stale(self) -> None:
        saved = {"url": "http://127.0.0.1:4097", "port": 4097, "password": "secret",
                 "pid": 111, "started": 1.0}
        captured = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def health(self):
                return False

        def fake_start(port, password, verbose, base=None):
            captured["port"] = port
            captured["password"] = password
            return {"url": saved["url"]}

        with mock.patch.object(team, "load_server", return_value=saved), mock.patch.object(
            team, "OpenCode", FakeClient
        ), mock.patch.object(team, "config_mtime", return_value=999.0), mock.patch.object(
            team, "_stop_and_wait"
        ), mock.patch.object(team, "_wait_port_free", return_value=True), mock.patch.object(
            team, "_start_server", side_effect=fake_start
        ):
            team.ensure_server(4096, verbose=False)
        self.assertEqual(captured["port"], 4097)
        self.assertEqual(captured["password"], "secret")


if __name__ == "__main__":
    unittest.main()