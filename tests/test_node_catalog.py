"""Catalog coverage, exact schemas, source evidence and example linking."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kb
import node_catalog


class LocalNode:
    """Combine two local images with a selectable strength."""
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("combined",)
    FUNCTION = "combine"
    CATEGORY = "image/combine"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",), "strength": ("FLOAT", {"default": 0.5, "min": 0, "max": 1})},
                "optional": {"mode": (["normal", "multiply"], {"tooltip": "Blend mode"})}, "hidden": {"prompt": "PROMPT"}}

    def combine(self, image, strength):
        """Example processing method. Indexing must never call this."""
        raise AssertionError("Must not execute the node")


class BrokenNode(LocalNode):
    @classmethod
    def INPUT_TYPES(cls):
        raise RuntimeError("Missing node dependency")


class ModernNode:
    @classmethod
    def GET_NODE_INFO_V1(cls):
        return {"name": "InternalName", "input": {"required": {}}, "output": ["IMAGE"], "description": "Modern image node"}


class CatalogTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.mapping = {"BlendExact": LocalNode, "BrokenExact": BrokenNode, "ModernAlias": ModernNode}
        for patcher in (patch.object(kb, "DB_PATH", os.path.join(temp.name, "kb.sqlite")),
                        patch.object(node_catalog, "registry", return_value=(self.mapping, {"BlendExact": "Friendly blend"}))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_full_schema_and_source_are_preserved(self):
        record = node_catalog.record("BlendExact")
        self.assertEqual(record["input"]["required"]["strength"][1]["default"], 0.5)
        self.assertEqual(record["input"]["hidden"]["prompt"], "PROMPT")
        self.assertEqual(record["output_name"], ["combined"])
        self.assertEqual(record["purpose"]["basis"], "source_docstring")
        self.assertIn("Combine two local images", record["purpose"]["text"])
        self.assertTrue(any(e["kind"] == "execution" and e["line"] > 0 for e in record["source_evidence"]))
        self.assertEqual(len(record["fingerprint"]), 64)

    def test_modern_alias_is_exact_and_unknown_is_unavailable(self):
        self.assertEqual(node_catalog.record("ModernAlias")["name"], "ModernAlias")
        self.assertFalse(node_catalog.record("Friendly blend")["available"])

    def test_coverage_exposes_failures_missing_and_unavailable(self):
        self.assertEqual(kb.index_node_schemas(), 2)
        coverage = kb.node_coverage()
        self.assertEqual(coverage["registered"], 3)
        self.assertEqual(coverage["indexed"], 2)
        self.assertEqual(coverage["failures"][0]["name"], "BrokenExact")
        self.mapping["AddedLater"] = LocalNode
        del self.mapping["ModernAlias"]
        coverage = kb.node_coverage()
        self.assertEqual(coverage["missing"], ["AddedLater"])
        self.assertEqual(coverage["unavailable"], ["ModernAlias"])

    def test_live_record_works_before_rebuild_and_is_not_a_snippet(self):
        with patch.object(LocalNode, "DESCRIPTION", "Detailed purpose " * 1000, create=True):
            result = kb.node_docs("BlendExact")
        self.assertTrue(result["verified"])
        self.assertGreater(len(result["node"]["description"]), 500)
        self.assertIn("optional", result["node"]["input"])
        self.assertFalse(kb.node_docs("BrokenExact")["verified"])
        self.assertFalse(kb.node_docs("NotInstalled")["verified"])

    def test_nested_examples_link_by_exact_type_and_connections(self):
        folder = os.path.join(self.root, "pack", "examples", "nested")
        os.makedirs(folder)
        workflow = {"nodes": [{"id": 1, "type": "LoadImage"}, {"id": 2, "type": "BlendExact", "title": "Main blend"}],
                    "links": [[5, 1, 0, 2, 0, "IMAGE"]]}
        path = os.path.join(folder, "example.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(workflow, handle)
        with patch.object(kb, "_custom_node_dirs", return_value=[self.root]):
            self.assertEqual(kb.index_examples(), 1)
        examples = kb.node_docs("BlendExact")["examples"]
        self.assertEqual(examples[0]["path"], path)
        self.assertEqual(examples[0]["incoming"][0]["source_type"], "LoadImage")
        self.assertEqual(kb.node_docs("Blend")["examples"], [])

    def test_api_examples_ignore_scalar_values_and_keep_links(self):
        examples = node_catalog.example_nodes({"1": {"class_type": "LoadImage", "inputs": {}},
            "2": {"class_type": "BlendExact", "inputs": {"image": ["1", 0], "strength": 0.5}}})
        self.assertEqual(examples[1]["incoming"], [{"source_type": "LoadImage", "source_slot": 0, "target_input": "image"}])


class _FlakyMapping:
    """Mapping that raises the registry-race RuntimeError on its first copy."""

    def __init__(self, data):
        self._data = dict(data)
        self.copies = 0

    def keys(self):
        self.copies += 1
        if self.copies == 1:
            raise RuntimeError("dictionary changed size during iteration")
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]

    def __len__(self):
        return len(self._data)



class _AlwaysFlakyMapping:
    def keys(self):
        raise RuntimeError("dictionary changed size during iteration")


class SnapshotTest(unittest.TestCase):
    def test_returns_copies_isolated_from_later_mutations(self):
        live = {"A": object()}
        names = {"A": "A"}
        with patch.object(node_catalog, "registry", return_value=(live, names)):
            mapping, name_snapshot = node_catalog.snapshot()
        live["B"] = object()
        names["B"] = "B"
        self.assertEqual(set(mapping), {"A"})
        self.assertEqual(name_snapshot, {"A": "A"})

    def test_retries_when_registry_is_mutated_during_copy(self):
        flaky = _FlakyMapping({"A": 1})
        with patch.object(node_catalog, "registry", return_value=(flaky, {})):
            mapping, _ = node_catalog.snapshot(attempts=3, delay=0)
        self.assertEqual(mapping, {"A": 1})
        self.assertGreaterEqual(flaky.copies, 2)

    def test_raises_when_it_never_settles(self):
        with patch.object(node_catalog, "registry", return_value=(_AlwaysFlakyMapping(), {})):
            with self.assertRaises(RuntimeError):
                node_catalog.snapshot(attempts=2, delay=0)
