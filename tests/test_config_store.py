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
    def test_strata_default_and_custom_server_persist(self):
        self.store.update({"provider": "strata", "base_url": ""})
        self.assertEqual(self.store.resolved()["base_url"], "http://p40:8080/v1")
        self.store.update({"base_url": "http://other-server:8080/v1"})
        restored = ConfigStore(self.path)
        self.assertEqual(restored.resolved()["provider"], "strata")
        self.assertEqual(restored.snapshot()["base_url"], "http://other-server:8080/v1")

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

    def test_embedding_secret_masking_and_blank_key_preservation(self):
        self.store.update({"embedding": {"provider": "openai", "base_url": "http://embed/v1",
                                          "model": "embed-v1", "api_key": "embed-secret"}})
        snapshot = self.store.update({"embedding": {"provider": "openai", "api_key": ""}})
        self.assertEqual(snapshot["embedding"]["api_key"], "")
        self.assertTrue(snapshot["embedding"]["api_key_configured"])
        self.assertEqual(self.store.resolved()["embedding"]["api_key"], "embed-secret")
        self.assertEqual(self.store.resolved()["embedding"]["base_url"], "http://embed/v1")

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

    def test_corrupt_history_is_preserved_on_save(self):
        history_path = self.store._history_path
        damaged = b'{"workflow": [not valid JSON]'
        with open(history_path, "wb") as handle:
            handle.write(damaged)

        with self.assertRaises(json.JSONDecodeError):
            self.store.set_history("workflow", [{"role": "user", "content": "new"}])

        with open(history_path, "rb") as handle:
            self.assertEqual(handle.read(), damaged)

    def test_invalid_history_shape_is_preserved_on_save(self):
        history_path = self.store._history_path
        damaged = b'{"workflow": {"not": "a message array"}}'
        with open(history_path, "wb") as handle:
            handle.write(damaged)

        with self.assertRaisesRegex(ValueError, "workflow keys to message arrays"):
            self.store.set_history("another", [{"role": "user", "content": "new"}])

        with open(history_path, "rb") as handle:
            self.assertEqual(handle.read(), damaged)

    def test_thinking_defaults_on_and_toggles(self):
        self.assertTrue(self.store.resolved()["thinking"]["enabled"])
        self.assertEqual(self.store.resolved()["thinking"]["level"], "auto")
        snapshot = self.store.update({"thinking": {"enabled": False}})
        self.assertFalse(snapshot["thinking"]["enabled"])
        self.assertFalse(self.store.resolved()["thinking"]["enabled"])
        self.assertEqual(self.store.resolved()["thinking"]["level"], "auto")
        self.store.update({"thinking": {"level": "HIGH"}})
        self.assertEqual(ConfigStore(self.path).resolved()["thinking"]["level"], "high")
        self.store.update({"thinking": {"level": "unrecognized"}})
        self.assertEqual(self.store.resolved()["thinking"]["level"], "auto")

    def test_experience_defaults(self):
        config = self.store.resolved()["experience"]
        self.assertTrue(config["enabled"])
        self.assertTrue(config["ask_approval"])
        self.assertEqual(config["recall_limit"], 0)

    def test_update_does_not_keep_caller_owned_values(self):
        values = {"kb": {"embed": {"model": "original"}}}
        self.store.update(values)
        values["kb"]["embed"]["model"] = "mutated"
        self.assertEqual(self.store.resolved()["kb"]["embed"]["model"], "original")
