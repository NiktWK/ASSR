"""Tests for runtime settings (per-role context-compaction thresholds)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import settings


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="settings-"))
        self.orig = (settings.RUN_DIR, settings.SETTINGS_FILE)
        settings.RUN_DIR = self.tmp
        settings.SETTINGS_FILE = self.tmp / "settings.json"

    def tearDown(self) -> None:
        settings.RUN_DIR, settings.SETTINGS_FILE = self.orig

    def test_defaults_when_no_file(self) -> None:
        thresholds = settings.compact_thresholds()
        self.assertEqual(thresholds["controller"], 150_000)
        self.assertEqual(thresholds["researcher"], 200_000)
        self.assertEqual(thresholds["developer"], 200_000)
        self.assertEqual(thresholds["tester"], 200_000)
        self.assertEqual(settings.compact_threshold("secretary"), 150_000)

    def test_set_and_persist(self) -> None:
        roles = settings.set_compact("developer", 123_000)
        self.assertEqual(roles, ["developer"])
        self.assertEqual(settings.compact_threshold("developer"), 123_000)
        data = json.loads(settings.SETTINGS_FILE.read_text("utf-8"))
        self.assertEqual(data["compact"]["developer"], 123_000)
        self.assertEqual(settings.compact_threshold("tester"), 200_000)

    def test_set_all_and_zero_disables(self) -> None:
        settings.set_compact("all", 0)
        self.assertTrue(all(v == 0 for v in settings.compact_thresholds().values()))
        settings.set_compact("all", 111_000)
        self.assertTrue(all(v == 111_000 for v in settings.compact_thresholds().values()))

    def test_unknown_role_and_bad_value(self) -> None:
        with self.assertRaises(ValueError):
            settings.set_compact("nope", 100_000)
        with self.assertRaises(ValueError):
            settings.set_compact("developer", 5_000)
        with self.assertRaises(ValueError):
            settings.set_compact("developer", "abc")

    def test_out_of_range_stored_value_means_off(self) -> None:
        settings.SETTINGS_FILE.write_text(
            json.dumps({"compact": {"developer": 1}}), "utf-8"
        )
        self.assertEqual(settings.compact_threshold("developer"), 0)

    def test_missing_role_in_file_keeps_default(self) -> None:
        settings.SETTINGS_FILE.write_text(
            json.dumps({"compact": {"developer": 120_000}}), "utf-8"
        )
        self.assertEqual(settings.compact_threshold("developer"), 120_000)
        self.assertEqual(settings.compact_threshold("controller"), 150_000)

    def test_format_tokens(self) -> None:
        self.assertEqual(settings.format_tokens(0), "выкл")
        self.assertEqual(settings.format_tokens(150_000), "150k")
        self.assertEqual(settings.format_tokens(200_000), "200k")


if __name__ == "__main__":
    unittest.main()
