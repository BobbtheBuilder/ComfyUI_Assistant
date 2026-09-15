"""Configuration persistence regressions, using temporary files only."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config_store import ConfigStore


class ConfigStoreTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = os.path.join(temp.name, "config.json")
        self.store = ConfigStore(self.path, os.path.join(temp.name, "history.json"))

    def test_partial_nested_update_preserves_siblings_and_secrets(self):
        self.store.update({"kb": {"embed": {"model": "embed-model", "enabled": False}},
                           "api_key": "test-secret", "websearch": {"api_key": "search-secret"}})
        snapshot = self.store.update({"kb": {"embed": {"enabled": True, "unknown": 1}},
                                      "api_key": "", "websearch": {"api_key": ""}})
        self.assertEqual(snapshot["kb"]["embed"], {"model": "embed-model", "enabled": True})
        self.assertEqual(snapshot["api_key"], "")
        self.assertTrue(snapshot["api_key_configured"])
        self.assertEqual(self.store.resolved()["api_key"], "test-secret")
        self.assertEqual(self.store.resolved()["websearch"]["api_key"], "search-secret")

    def test_load_fills_nested_defaults_and_ignores_invalid_sections(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"kb": {"embed": {"model": "saved", "unknown": 1}}, "debug": None}, handle)
        config = ConfigStore(self.path).resolved()
        self.assertEqual(config["kb"]["embed"], {"model": "saved", "enabled": True})
        self.assertEqual(config["debug"], {"enabled": False})
        self.store.update({"kb": {"embed": None}})
        self.assertIsInstance(self.store.resolved()["kb"]["embed"], dict)

    def test_failed_save_does_not_change_active_configuration(self):
        previous = self.store.resolved()
        with patch("config_store._write_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.update({"model": "unsaved"})
        self.assertEqual(self.store.resolved(), previous)

    def test_update_does_not_keep_caller_owned_values(self):
        values = {"kb": {"embed": {"model": "original"}}}
        self.store.update(values)
        values["kb"]["embed"]["model"] = "mutated"
        self.assertEqual(self.store.resolved()["kb"]["embed"]["model"], "original")
