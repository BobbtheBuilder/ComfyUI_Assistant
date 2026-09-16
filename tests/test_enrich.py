"""Offline tests for the knowledge enrichment pipeline (no network, stubbed model)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

NODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NODE_DIR not in sys.path:
    sys.path.insert(0, NODE_DIR)

import enrich  # noqa: E402
import knowledge  # noqa: E402


def fake_cls(module: str, category: str = "Test", description: str = "",
             inputs: tuple = ("model",), outputs: tuple = ("MODEL",)):
    class Node:
        RELATIVE_PYTHON_MODULE = module
        CATEGORY = category
        DESCRIPTION = description
        RETURN_TYPES = outputs
        OUTPUT_NODE = False
        INPUT_TYPES = staticmethod(lambda: {"required": {name: ["MODEL"] for name in inputs}})
    return Node


MAPPING = {"PackLoader": fake_cls("custom_nodes.MyPack.nodes"), "PackSampler": fake_cls("custom_nodes.MyPack.nodes")}
NAMES = {"PackLoader": "Pack Loader", "PackSampler": "Pack Sampler"}


class EnrichTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = knowledge.DB_PATH
        knowledge.DB_PATH = os.path.join(self.tmp, "k.sqlite")
        knowledge.index_knowledge()

    def tearDown(self):
        knowledge.DB_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        conn = knowledge._connect()
        knowledge._init(conn)
        return conn

    def test_local_facts_build_pack_graph_and_queue(self):
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {"PackLoader": "https://github.com/me/mypack"}):
            result = enrich.index_local_facts()
        self.assertEqual(result["nodes"], 2)
        self.assertEqual(result["packs"], 1)
        conn = self._conn()
        try:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM entities WHERE id = 'pack:MyPack'").fetchone())
            props = dict(conn.execute("SELECT key, value FROM properties WHERE entity_id = 'node:PackLoader'").fetchall())
            self.assertEqual(props.get("pack"), "MyPack")
            self.assertEqual(conn.execute("SELECT status FROM enrichment WHERE scope_id = 'pack:MyPack'").fetchone()[0], "pending")
            repo = conn.execute("SELECT value FROM properties WHERE entity_id = 'pack:MyPack' AND key = 'repo'").fetchone()
            self.assertEqual(repo[0], "https://github.com/me/mypack")
        finally:
            conn.close()

    def test_enrich_pack_maps_llm_result_to_nodes(self):
        model_json = json.dumps({
            "pack": {"purpose": "Loads stuff", "summary": "s"},
            "nodes": [{
                "node": "PackLoader", "purpose": "loads the model", "roles": ["model_loader"],
                "architecture_family": ["mypack"],
                "compat": [{"key": "model_role", "op": "in", "value": ["diffusion_model"], "generic": True}],
                "confidence": 0.7,
            }],
        })
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}), \
             patch.object(enrich, "_pack_docs", lambda name: "docs"), \
             patch.object(enrich, "_web_research", lambda name, repo: ""), \
             patch.object(enrich, "_complete", lambda messages, max_tokens=0: model_json), \
             patch.object(enrich, "_config", lambda: {"model": "test-model"}), \
             patch.object(enrich, "_enrich_config", lambda: {"web": False, "model": "test-model"}):
            enrich.index_local_facts()
            out = enrich.enrich_pack("pack:MyPack", web=False)
        self.assertEqual(out["nodes"], 1)
        conn = self._conn()
        try:
            rows = conn.execute("SELECT key, value, source FROM properties WHERE entity_id = 'node:PackLoader' AND source LIKE 'llm:%'").fetchall()
            self.assertEqual({key: value for key, value, _ in rows}.get("purpose"), "loads the model")
            self.assertIn("model_loader", [value for key, value, _ in rows if key == "roles"])
            self.assertIsNotNone(conn.execute("SELECT 1 FROM requirements WHERE scope_id = 'PackLoader' AND provenance LIKE 'llm:%'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM evidence WHERE entity = 'node:PackLoader' AND source LIKE 'llm:%'").fetchone())
            self.assertEqual(conn.execute("SELECT status FROM enrichment WHERE scope_id = 'pack:MyPack'").fetchone()[0], "done")
        finally:
            conn.close()

    def test_pack_docs_and_web_are_added_to_evidence(self):
        calls = {}

        def fake_fetch(url):
            calls["url"] = url
            return "# README\nLoads models."

        class FakeSearch:
            @staticmethod
            async def search(config, query, count=None):
                calls["query"] = query
                return [{"title": "t", "url": "u", "snippet": "snip"}]

        with patch.object(enrich, "_fetch_text", fake_fetch), \
             patch.object(enrich, "_config", lambda: {}), \
             patch.object(enrich, "_enrich_config", lambda: {"web": True, "max_web_queries_per_pack": 3}), \
             patch.dict(sys.modules, {"websearch": FakeSearch}):
            text = enrich._web_research("MyPack", "https://github.com/me/mypack")
        self.assertIn("README", text)
        self.assertIn("snip", text)
        self.assertTrue(calls["url"].startswith("https://raw.githubusercontent.com/me/mypack/"))

    def test_freshness_requeues_when_pack_changes(self):
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}):
            enrich.index_local_facts()
        conn = self._conn()
        try:
            conn.execute("UPDATE enrichment SET status = 'done' WHERE scope_id = 'pack:MyPack'")
            conn.commit()
        finally:
            conn.close()
        changed = dict(MAPPING)
        changed["PackExtra"] = fake_cls("custom_nodes.MyPack.nodes")
        with patch.object(enrich, "_registry", lambda: (changed, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}):
            enrich.index_local_facts()
        conn = self._conn()
        try:
            self.assertEqual(conn.execute("SELECT status FROM enrichment WHERE scope_id = 'pack:MyPack'").fetchone()[0], "pending")
        finally:
            conn.close()

    def test_research_entity_targets_pack(self):
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}):
            enrich.index_local_facts()
        with patch.object(enrich, "enrich_pack", return_value={"nodes": 2}) as mock:
            result = enrich.research_entity("pack:MyPack", web=True)
        self.assertTrue(result["ok"])
        self.assertTrue(mock.called)
        self.assertEqual(mock.call_args[0][0], "pack:MyPack")


if __name__ == "__main__":
    unittest.main()


class EnrichSchedulingTest(EnrichTest):
    def _queue_one_pack(self):
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}):
            enrich.index_local_facts()

    def test_run_waits_for_index_build(self):
        self._queue_one_pack()
        with patch.object(enrich, "_enrich_config", lambda: {"enabled": True, "web": False, "idle_only": True, "model": "m"}), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_kb_busy", lambda: True), \
             patch.object(enrich, "_chat_busy", lambda: False):
            result = enrich.run(limit=1, web=False)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "index build")

    def test_run_waits_for_chat(self):
        self._queue_one_pack()
        with patch.object(enrich, "_enrich_config", lambda: {"enabled": True, "web": False, "idle_only": True, "model": "m"}), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_kb_busy", lambda: False), \
             patch.object(enrich, "_chat_busy", lambda: True):
            result = enrich.run(limit=1, web=False)
        self.assertEqual(result["reason"], "chat")

    def test_idle_only_false_runs_while_busy(self):
        self._queue_one_pack()
        with patch.object(enrich, "_enrich_config", lambda: {"enabled": True, "web": False, "idle_only": False, "model": "m"}), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_kb_busy", lambda: True), \
             patch.object(enrich, "enrich_pack", return_value={"nodes": 1}):
            result = enrich.run(limit=1, web=False)
        self.assertEqual(len(result["processed"]), 1)

    def test_stop_halts_processing(self):
        self._queue_one_pack()
        enrich._stop.set()
        try:
            result = enrich.run(limit=1, web=False, ignore_busy=True)
        finally:
            enrich._stop.clear()
        self.assertEqual(result["processed"], [])


class EnrichQualityTest(EnrichTest):
    def test_pack_nodes_includes_nodes_beyond_sixty(self):
        mapping = {f"Node{i}": fake_cls("custom_nodes.BigPack.nodes") for i in range(85)}
        with patch.object(enrich, "_registry", return_value=(mapping, {})):
            nodes = enrich._pack_nodes("BigPack")
        self.assertEqual(len(nodes), 85)
        self.assertEqual(nodes[-1]["type"], "Node84")

    def _queue_one_pack(self):
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}):
            enrich.index_local_facts()

    def test_enrich_pack_batches_large_packs(self):
        big = {f"Big{i}": fake_cls("custom_nodes.BigPack.nodes") for i in range(25)}
        names = {name: name for name in big}
        calls = []

        def fake_complete(messages, response_format=None):
            calls.append(json.loads(messages[1]["content"].split("\n\nReturn JSON only.")[0]))
            return json.dumps({"pack": {"purpose": "p", "summary": "s"}, "nodes": []})

        with patch.object(enrich, "_registry", lambda: (big, names)), \
             patch.object(enrich, "_node_repo_map", lambda: {}), \
             patch.object(enrich, "_pack_docs", lambda name: ""), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_enrich_config", lambda: {"web": False, "batch_size": 20, "model": "m"}), \
             patch.object(enrich, "_complete", fake_complete):
            enrich.index_local_facts()
            out = enrich.enrich_pack("pack:BigPack", web=False)
        self.assertEqual(out["batches"], 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[0]["nodes"]), 20)
        self.assertEqual(len(calls[1]["nodes"]), 5)

    def test_complete_requests_json_format_without_token_cap(self):
        recorded = {}

        class FakeProviders:
            @staticmethod
            async def complete(config, messages, max_tokens=None, response_format=None):
                recorded["max_tokens"] = max_tokens
                recorded["response_format"] = response_format
                return "{}"

        with patch.object(enrich, "_providers", lambda: FakeProviders), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_enrich_config", lambda: {"max_tokens": 0}):
            enrich._complete([{"role": "user", "content": "x"}])
        self.assertEqual(recorded["response_format"], enrich.JSON_FORMAT)
        self.assertIsNone(recorded["max_tokens"])

    def test_compat_keys_restricted_and_confidence_clamped(self):
        model_json = json.dumps({"pack": {}, "nodes": [{
            "node": "PackLoader", "purpose": "loads", "roles": ["model_loader"],
            "compat": [
                {"key": "requires_civitai_account", "op": "exists", "value": [], "generic": False},
                {"key": "model_role", "op": "in", "value": ["diffusion_model"], "generic": True},
            ], "confidence": 0.98,
        }]})
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}), \
             patch.object(enrich, "_pack_docs", lambda name: ""), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_enrich_config", lambda: {"web": False, "model": "m"}), \
             patch.object(enrich, "_complete", lambda messages, response_format=None: model_json):
            enrich.index_local_facts()
            enrich.enrich_pack("pack:MyPack", web=False)
        conn = self._conn()
        try:
            reqs = conn.execute("SELECT predicate FROM requirements WHERE scope_id='PackLoader' AND provenance LIKE 'llm:%'").fetchall()
            keys = {json.loads(r[0])["key"] for r in reqs}
            self.assertEqual(keys, {"model_role"})
            conf = conn.execute("SELECT confidence FROM properties WHERE entity_id='node:PackLoader' AND key='purpose'").fetchone()[0]
            self.assertLessEqual(conf, enrich.MAX_LLM_CONFIDENCE)
            notes = [r[0] for r in conn.execute("SELECT value FROM properties WHERE entity_id='node:PackLoader' AND key='side_effects'").fetchall()]
            self.assertTrue(any("requires_civitai_account" in note for note in notes))
        finally:
            conn.close()

    def test_fail_pack_skips_after_max_attempts_and_requeues(self):
        self._queue_one_pack()
        with patch.object(enrich, "_enrich_config", lambda: {"max_attempts": 2}):
            enrich._fail_pack("pack:MyPack", "boom")
            enrich._fail_pack("pack:MyPack", "boom")
        self.assertEqual(enrich.coverage()["skipped"], 1)
        self.assertEqual(enrich.requeue_skipped(), 1)
        self.assertEqual(enrich.coverage()["pending"], 1)

    def test_normalize_architecture(self):
        self.assertEqual(enrich.normalize_architecture("Flux.2 Klein"), "flux2")
        self.assertEqual(enrich.normalize_architecture("SDXL"), "sdxl")
        self.assertEqual(enrich.normalize_architecture("Stable Diffusion (SD1.5, SDXL, SD3)"), "sd15")
        self.assertEqual(enrich.normalize_architecture("dlss5"), "")
        self.assertEqual(enrich.normalize_architecture("generic utility"), "")

    def test_web_evidence_is_stored(self):
        self._queue_one_pack()
        enrich._store_web_evidence("MyPack", "# README\nloads models")
        conn = self._conn()
        try:
            row = conn.execute("SELECT source_type, trust, entity FROM evidence WHERE entity='pack:MyPack'").fetchone()
            self.assertEqual(tuple(row), ("web", "WEB_UNVERIFIED", "pack:MyPack"))
        finally:
            conn.close()

    def test_complete_falls_back_when_response_format_rejected(self):
        seen = []

        class FakeProviders:
            @staticmethod
            async def complete(config, messages, max_tokens=None, response_format=None):
                seen.append(response_format)
                if response_format is not None:
                    raise RuntimeError("Provider returned HTTP 400: 'response_format.type' must be 'json_schema' or 'text'")
                return "{}"

        with patch.object(enrich, "_providers", lambda: FakeProviders), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_enrich_config", lambda: {"max_tokens": 0, "json_mode": "schema"}), \
             patch.object(enrich, "_debug_log", lambda *a, **k: None):
            out = enrich._complete([{"role": "user", "content": "x"}])
        self.assertEqual(out, "{}")
        self.assertEqual(seen[0], enrich.JSON_FORMAT)
        self.assertIsNone(seen[1])

    def test_json_mode_none_disables_response_format(self):
        seen = []

        class FakeProviders:
            @staticmethod
            async def complete(config, messages, max_tokens=None, response_format=None):
                seen.append(response_format)
                return "{}"

        with patch.object(enrich, "_providers", lambda: FakeProviders), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_enrich_config", lambda: {"json_mode": "none"}):
            enrich._complete([{"role": "user", "content": "x"}])
        self.assertIsNone(seen[0])

    def test_v2_migration_requeues_failed_packs(self):
        self._queue_one_pack()
        conn = self._conn()
        try:
            conn.execute("UPDATE enrichment SET status='error', attempts=2 WHERE scope_id='pack:MyPack'")
            conn.execute("DELETE FROM meta WHERE key='enrich_quality_v2'")
            conn.commit()
        finally:
            conn.close()
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}):
            enrich.index_local_facts()
        conn = self._conn()
        try:
            row = conn.execute("SELECT status, attempts FROM enrichment WHERE scope_id='pack:MyPack'").fetchone()
            self.assertEqual(tuple(row), ("pending", 0))
        finally:
            conn.close()


class EnrichArchTest(EnrichTest):
    def _run_pack(self, model_json):
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}), \
             patch.object(enrich, "_pack_docs", lambda name: ""), \
             patch.object(enrich, "_config", lambda: {"model": "m"}), \
             patch.object(enrich, "_enrich_config", lambda: {"web": False, "model": "m"}), \
             patch.object(enrich, "_complete", lambda messages: model_json):
            enrich.index_local_facts()
            enrich.enrich_pack("pack:MyPack", web=False)

    def test_architecture_family_is_informational_not_a_requirement(self):
        self._run_pack(json.dumps({"pack": {}, "nodes": [{
            "node": "PackLoader", "roles": ["model_loader"], "architecture_family": ["flux1"],
            "compat": [{"key": "architecture_family", "op": "in", "value": ["flux1"], "generic": False}],
            "confidence": 0.6,
        }]}))
        conn = self._conn()
        try:
            kept = conn.execute("SELECT 1 FROM properties WHERE entity_id='node:PackLoader' "
                                "AND key='architecture_family' AND value='flux1'").fetchone()
            self.assertIsNotNone(kept)
            restricted = conn.execute("SELECT 1 FROM requirements WHERE scope_id='PackLoader' "
                                      "AND predicate LIKE '%architecture_family%'").fetchone()
            self.assertIsNone(restricted)
        finally:
            conn.close()

    def test_v3_migration_removes_llm_architecture_requirements(self):
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}):
            enrich.index_local_facts()
        conn = self._conn()
        try:
            conn.execute("INSERT INTO requirements (scope_kind, scope_id, task, predicate, state_match, "
                         "state_mismatch, confidence, provenance, updated_at) "
                         "VALUES ('node', 'PackLoader', '', ?, 'INFERRED_COMPATIBLE', 'INFERRED_INCOMPATIBLE', "
                         "0.6, 'llm:m', 0)",
                         (json.dumps({"key": "architecture_family", "op": "in", "value": ["flux1"]}),))
            conn.execute("DELETE FROM meta WHERE key='enrich_quality_v3'")
            conn.commit()
        finally:
            conn.close()
        with patch.object(enrich, "_registry", lambda: (MAPPING, NAMES)), \
             patch.object(enrich, "_node_repo_map", lambda: {}):
            enrich.index_local_facts()
        conn = self._conn()
        try:
            left = conn.execute("SELECT COUNT(*) FROM requirements "
                                "WHERE provenance LIKE 'llm:%' AND predicate LIKE '%architecture_family%'").fetchone()[0]
            self.assertEqual(left, 0)
        finally:
            conn.close()
