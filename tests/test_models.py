"""Tests for model resolution and server restart helpers in team.py."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import team


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