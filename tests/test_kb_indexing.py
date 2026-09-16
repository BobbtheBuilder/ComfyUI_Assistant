"""Offline tests for the KB indexer helpers (node schema text + workflow summaries)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

NODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NODE_DIR not in sys.path:
    sys.path.insert(0, NODE_DIR)

import kb  # noqa: E402


class SpecTextTest(unittest.TestCase):
    def test_plain_type_with_options(self):
        text = kb._spec_text(["INT", {"default": 20, "min": 1, "max": 10000}])
        self.assertTrue(text.startswith("INT"))
        self.assertIn("default=20", text)
        self.assertIn("max=10000", text)

    def test_enum_options(self):
        self.assertEqual(kb._spec_text([["euler", "heun"], {}]), "enum(euler, heun)")

    def test_tooltip(self):
        self.assertIn("tooltip=hello", kb._spec_text(["MODEL", {"tooltip": "hello"}]))

    def test_malformed(self):
        self.assertEqual(kb._spec_text("STRING"), "STRING")


class NodeContentTest(unittest.TestCase):
    def test_includes_inputs_outputs_and_meta(self):
        info = {
            "name": "KSampler",
            "display_name": "KSampler",
            "category": "sampling",
            "python_module": "nodes",
            "description": "Samples the latent.",
            "input": {
                "required": {"model": ["MODEL", {}], "seed": ["INT", {"default": 0}]},
                "optional": {"denoise": ["FLOAT", {"default": 1.0}]},
            },
            "output": ["LATENT"],
            "output_name": ["LATENT"],
            "output_node": False,
        }
        content = kb._node_content(info)
        self.assertIn("KSampler (KSampler)", content)
        self.assertIn("input[required] model: MODEL", content)
        self.assertIn("input[optional] denoise: FLOAT default=1.0", content)
        self.assertIn("output LATENT: LATENT", content)


class WorkflowSummaryTest(unittest.TestCase):
    def test_ui_format(self):
        data = {"nodes": [{"type": "KSampler", "widgets_values": [123, 8]}, {"type": "KSampler"}, {"type": "VAEDecode"}]}
        summary = kb._workflow_summary(data)
        self.assertIn("KSampler x2", summary)
        self.assertIn("VAEDecode", summary)
        self.assertIn("Settings:", summary)

    def test_api_format(self):
        data = {"1": {"class_type": "LoadImage", "inputs": {"image": "example.png"}}}
        summary = kb._workflow_summary(data)
        self.assertIn("LoadImage", summary)
        self.assertIn("image=example.png", summary)

    def test_unknown_shape(self):
        self.assertEqual(kb._workflow_summary({"foo": "bar"}), "")


class BuildFingerprintTest(unittest.TestCase):
    def _fingerprint(self, mapping, flags=None, manager="", version=None):
        patches = [
            patch.object(kb.node_catalog, "registry", return_value=(mapping, {})),
            patch.object(kb, "_manager_dir", return_value=manager),
            patch.object(kb, "_kb_config", return_value=flags or {}),
        ]
        if version is not None:
            patches.append(patch.object(kb, "KB_BUILD_VERSION", version))
        for patcher in patches:
            patcher.start()
        try:
            return kb._build_fingerprint()
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def test_stable_for_identical_inputs(self):
        self.assertEqual(self._fingerprint({"A": 1}), self._fingerprint({"A": 1}))

    def test_node_set_change_changes_fingerprint(self):
        self.assertNotEqual(self._fingerprint({"A": 1}), self._fingerprint({"A": 1, "B": 2}))

    def test_flag_change_changes_fingerprint(self):
        self.assertNotEqual(
            self._fingerprint({"A": 1}, {"examples": True}),
            self._fingerprint({"A": 1}, {"examples": False}),
        )

    def test_version_change_changes_fingerprint(self):
        self.assertNotEqual(self._fingerprint({"A": 1}, version=1), self._fingerprint({"A": 1}, version=2))

    def test_manager_file_change_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "custom-node-list.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("[]")
            before = self._fingerprint({"A": 1}, manager=tmp)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("[1, 2, 3]")
            after = self._fingerprint({"A": 1}, manager=tmp)
        self.assertNotEqual(before, after)

    def test_survives_registry_failure(self):
        with patch.object(kb.node_catalog, "registry", side_effect=RuntimeError("no nodes")), \
                patch.object(kb, "_manager_dir", return_value=""), \
                patch.object(kb, "_kb_config", return_value={}):
            value = kb._build_fingerprint()
        self.assertRegex(value, r"^[0-9a-f]{64}$")


class BuildSkipTest(unittest.TestCase):
    def _run(self, stored, force=False, embeddings_error=False):
        embeddings_patch = (
            patch.object(kb, "index_embeddings", side_effect=RuntimeError("embed down"))
            if embeddings_error else patch.object(kb, "index_embeddings")
        )
        with patch.object(kb, "_connect", side_effect=lambda: MagicMock()), \
                patch.object(kb, "_init"), \
                patch.object(kb, "_build_fingerprint", return_value="FP"), \
                patch.object(kb, "_meta_get", side_effect=lambda key: stored if key == "build_fingerprint" else ""), \
                patch.object(kb, "_meta_set") as meta_set, \
                patch.object(kb, "_refresh_counts"), \
                patch.object(kb, "_freelist_pages", return_value=0), \
                patch.object(kb, "compact"), \
                patch.object(kb, "_debug_log"), \
                patch.object(kb, "_kb_config", return_value={"examples": False, "registry": False, "extended_official": False}), \
                patch.object(kb, "index_local") as index_local, \
                patch.object(kb, "index_node_schemas") as index_node_schemas, \
                patch.object(kb, "index_examples") as index_examples, \
                patch.object(kb, "index_registry") as index_registry, \
                patch.object(kb, "index_extended_official") as index_extended, \
                embeddings_patch as index_embeddings:
            kb.build(force=force, auto_official=False)
        return {
            "meta_set": meta_set, "index_local": index_local, "index_node_schemas": index_node_schemas,
            "index_examples": index_examples, "index_registry": index_registry,
            "index_extended": index_extended, "index_embeddings": index_embeddings,
        }

    def test_matching_fingerprint_skips_rebuild(self):
        mocks = self._run("FP")
        mocks["index_local"].assert_not_called()
        mocks["index_node_schemas"].assert_not_called()
        mocks["index_embeddings"].assert_not_called()
        mocks["meta_set"].assert_not_called()
        self.assertEqual(kb._state["phase"], "Ready")
        self.assertFalse(kb._state["building"])

    def test_missing_fingerprint_runs_full_build(self):
        mocks = self._run("")
        mocks["index_local"].assert_called_once()
        mocks["index_node_schemas"].assert_called_once()
        mocks["index_embeddings"].assert_called_once()
        mocks["meta_set"].assert_called_once_with("build_fingerprint", "FP")

    def test_force_rebuilds_even_when_fingerprint_matches(self):
        mocks = self._run("FP", force=True)
        mocks["index_local"].assert_called_once()
        mocks["index_node_schemas"].assert_called_once()

    def test_records_fingerprint_even_if_embeddings_fail(self):
        mocks = self._run("", embeddings_error=True)
        mocks["meta_set"].assert_called_once_with("build_fingerprint", "FP")
        self.assertIn("embeddings", kb._state["error"])


if __name__ == "__main__":
    unittest.main()
