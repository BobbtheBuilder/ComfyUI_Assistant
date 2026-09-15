"""Offline tests for the KB indexer helpers (node schema text + workflow summaries)."""

from __future__ import annotations

import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
