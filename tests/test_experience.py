"""Offline tests for the workflow-experience store (reference evidence, not rules)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

NODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NODE_DIR not in sys.path:
    sys.path.insert(0, NODE_DIR)

import experience  # noqa: E402


class ExperienceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._original = experience.DB_PATH
        experience.DB_PATH = os.path.join(self.tmp, "experience.sqlite")

    def tearDown(self):
        experience.DB_PATH = self._original

    def test_records_fragment_and_defaults_to_pending(self):
        result = experience.add_experience({
            "kind": "success",
            "task": "basic qwen edit",
            "prompt_id": "p1",
            "fragment": {"nodes": [{"id": 1, "type": "LoadImage"}, {"id": 2, "type": "KSampler"}]},
            "execution_status": "success",
        })
        self.assertEqual(result["kind"], "success")
        self.assertEqual(result["verdict"], "pending")
        stored = experience.list_experiences()[0]
        self.assertEqual(stored["node_types"], ["LoadImage", "KSampler"])
        self.assertEqual(stored["prompt_id"], "p1")

    def test_failure_classification(self):
        self.assertEqual(experience.classify_failure(exception_message="CUDA out of memory"), "oom")
        self.assertEqual(
            experience.classify_failure(exception_message="No such file or directory: model.safetensors"),
            "missing_file",
        )
        self.assertEqual(experience.classify_failure(node_errors={"1": {"input": "x"}}), "invalid_connection")
        self.assertEqual(experience.classify_failure(exception_message="boom"), "runtime_error")
        self.assertEqual(experience.classify_failure(), "unknown")

    def test_verdict_rejection_marks_unsatisfactory_failure(self):
        result = experience.add_experience({
            "kind": "success",
            "task": "edit",
            "fragment": {"nodes": [{"id": 1, "type": "KSampler"}]},
        })
        self.assertTrue(experience.update_verdict(result["id"], "rejected", "wrong image", "unsatisfactory"))
        stored = experience.list_experiences()[0]
        self.assertEqual(stored["verdict"], "rejected")
        self.assertEqual(stored["kind"], "failure")
        self.assertEqual(stored["failure_kind"], "unsatisfactory")

    def test_semantic_search_and_dimension_guard(self):
        good = experience.add_experience({
            "kind": "success",
            "task": "dark moody portrait",
            "fragment": {"nodes": [{"id": 1, "type": "KSampler"}]},
        })
        experience.add_experience({
            "kind": "success",
            "task": "bright cheerful landscape",
            "fragment": {"nodes": [{"id": 2, "type": "KSampler"}]},
        })
        newest = experience.list_experiences()[0]
        self.assertTrue(experience.set_vector(good["id"], [1.0, 0.0]))
        experience.set_vector(newest["id"], [0.0, 1.0])
        results = experience.search("", 8, [0.9, 0.1])
        self.assertTrue(results)
        self.assertEqual(results[0]["task"], "dark moody portrait")
        self.assertEqual(experience.search("", 8, [0.1, 0.2, 0.3]), [])

    def test_relevant_prefers_approved_successes(self):
        approved = experience.add_experience({
            "kind": "success",
            "task": "qwen image edit workflow",
            "fragment": {"nodes": [{"id": 1, "type": "KSampler"}]},
        })
        experience.update_verdict(approved["id"], "approved")
        experience.add_experience({
            "kind": "failure",
            "task": "qwen image edit workflow",
            "execution_status": "error",
            "failure_kind": "oom",
            "error": "CUDA out of memory",
            "fragment": {"nodes": [{"id": 2, "type": "KSampler"}]},
        })
        results = experience.relevant("qwen image edit workflow", 5)
        self.assertTrue(results)
        self.assertEqual(results[0]["kind"], "success")
        self.assertEqual(results[0]["verdict"], "approved")

    def test_retention_prunes_old_records(self):
        with patch.object(experience, "MAX_RECORDS", 3):
            for index in range(5):
                experience.add_experience({
                    "kind": "success",
                    "task": f"task {index}",
                    "fragment": {"nodes": [{"id": index, "type": "KSampler"}]},
                })
        stored = experience.list_experiences()
        self.assertEqual(len(stored), 3)
        self.assertEqual([item["task"] for item in stored], ["task 4", "task 3", "task 2"])

    def test_revalidate_flags_missing_and_changed_nodes(self):
        fragment = {"nodes": [{"id": 1, "type": "Stable"}, {"id": 2, "type": "Gone"}]}
        records = {
            "Stable": {"available": True, "schema_error": "", "fingerprint": "new-hash"},
        }
        with patch.object(experience.node_catalog, "record", side_effect=lambda node_type: records.get(
                node_type, {"available": False})):
            report = experience.revalidate(fragment, {"Stable": "old-hash"})
        by_type = {item["type"]: item for item in report}
        self.assertTrue(by_type["Stable"]["installed"])
        self.assertTrue(by_type["Stable"]["schema_changed"])
        self.assertFalse(by_type["Gone"]["installed"])


if __name__ == "__main__":
    unittest.main()
