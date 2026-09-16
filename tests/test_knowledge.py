"""Offline tests for the open-world compatibility engine."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

NODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NODE_DIR not in sys.path:
    sys.path.insert(0, NODE_DIR)

import knowledge  # noqa: E402


class KnowledgeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_path = knowledge.DB_PATH
        knowledge.DB_PATH = os.path.join(self.tmp, "knowledge.sqlite")
        knowledge.index_knowledge()

    def tearDown(self):
        knowledge.DB_PATH = self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)


class SeedTest(KnowledgeTestBase):
    def test_schema_version_recorded(self):
        conn = knowledge._connect()
        try:
            knowledge._init(conn)
            self.assertEqual(knowledge._schema_version(conn), knowledge.SCHEMA_VERSION)
        finally:
            conn.close()

    def test_alias_resolution(self):
        self.assertEqual(knowledge.resolve("Klein 9B"), "flux2.klein.9b")
        self.assertEqual(knowledge.resolve("flux2 klein"), "flux2.klein.9b")
        self.assertEqual(knowledge.resolve("FLUX.2 Klein 9B"), "flux2.klein.9b")

    def test_resolve_unknown_returns_empty(self):
        self.assertEqual(knowledge.resolve("totally unseen model zzz"), "")

    def test_confirmed_edges_win(self):
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "KSampler")["state"],
                         knowledge.CONFIRMED_COMPATIBLE)
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "CheckpointLoaderSimple")["state"],
                         knowledge.CONFIRMED_INCOMPATIBLE)

    def test_inferred_compatible_from_properties(self):
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "EmptyFlux2LatentImage")["state"],
                         knowledge.INFERRED_COMPATIBLE)
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "Flux2Scheduler")["state"],
                         knowledge.INFERRED_COMPATIBLE)

    def test_generic_when_no_specific_requirements(self):
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "VAEDecode")["state"], knowledge.GENERIC)
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "SaveImage")["state"], knowledge.GENERIC)

    def test_property_mismatch_is_incompatible(self):
        result = knowledge.evaluate("flux2.klein.9b", "DualCLIPLoader")
        self.assertEqual(result["state"], knowledge.INFERRED_INCOMPATIBLE)
        self.assertIn("encoder_family", result["reason"])


class OpenWorldTest(KnowledgeTestBase):
    def test_missing_property_is_unknown_not_incompatible(self):
        knowledge.add_entity("demo.model", "model", "Demo Model")
        knowledge.add_requirement("node", "MysteryNode",
                                 {"key": "special_format", "op": "in", "value": ["x"], "required": True})
        self.assertEqual(knowledge.evaluate("demo.model", "MysteryNode")["state"], knowledge.UNKNOWN)

    def test_unseen_model_is_evaluated_not_assumed(self):
        knowledge.add_entity("flux3.mega.20b", "model", "Flux3 Mega 20B")
        knowledge.set_property("flux3.mega.20b", "encoder_family", "qwen3")
        knowledge.set_property("flux3.mega.20b", "latent_format", "flux2")
        # A future model matches generic + specific requirements without any curated list entry.
        self.assertEqual(knowledge.evaluate("flux3.mega.20b", "CLIPLoader")["state"], knowledge.GENERIC)
        self.assertEqual(knowledge.evaluate("flux3.mega.20b", "EmptyFlux2LatentImage")["state"],
                         knowledge.INFERRED_COMPATIBLE)
        # A constraint it violates is still caught.
        self.assertEqual(knowledge.evaluate("flux3.mega.20b", "DualCLIPLoader")["state"],
                         knowledge.INFERRED_INCOMPATIBLE)


class PacketTest(KnowledgeTestBase):
    NODES = ["UNETLoader", "CLIPLoader", "DualCLIPLoader", "CheckpointLoaderSimple", "KSampler",
             "VAEDecode", "EmptyFlux2LatentImage", "Flux2Scheduler", "MysteryNode"]

    def test_packet_blocks_incompatible_and_warns_unknown(self):
        knowledge.add_requirement("node", "MysteryNode",
                                 {"key": "absent_key", "op": "in", "value": ["x"], "required": True})
        packet = knowledge.get_build_context("Klein 9B", "t2i", self.NODES)
        self.assertTrue(packet["resolved"])
        self.assertEqual(packet["target_model"]["id"], "flux2.klein.9b")
        entries = {entry["node"]: entry for entry in packet["compatible_nodes"]}
        blocked = {entry["node"] for entry in packet["exclusions"]}
        unknown = {entry["node"] for entry in packet["unknown_nodes"]}
        self.assertIn("UNETLoader", entries)
        self.assertIn("EmptyFlux2LatentImage", entries)
        self.assertEqual(entries["UNETLoader"]["warn"], False)
        self.assertIn("DualCLIPLoader", blocked)
        self.assertIn("CheckpointLoaderSimple", blocked)
        # Unknown nodes are usable but flagged as unverified.
        self.assertIn("MysteryNode", entries)
        self.assertEqual(entries["MysteryNode"]["state"], knowledge.UNKNOWN)
        self.assertTrue(entries["MysteryNode"]["warn"])
        self.assertIn("MysteryNode", unknown)
        self.assertFalse(set(entries) & blocked)
        for entry in packet["compatible_nodes"]:
            self.assertNotIn(entry["state"], knowledge.BLOCKED_STATES)
        self.assertIn("warn_states", packet["policy"])

    def test_unresolved_model_reports_instead_of_guessing(self):
        packet = knowledge.get_build_context("nonexistent model qqq", "t2i", self.NODES)
        self.assertFalse(packet["resolved"])
        self.assertIn("do not guess", packet["reason"].lower())

    def test_default_packet_uses_relevant_nodes_without_truncating(self):
        for index in range(100):
            name = f"AuditNode{index}"
            knowledge.add_entity(f"node:{name}", "node")
            knowledge.set_property(f"node:{name}", "roles", "image_utility")
        packet = knowledge.get_build_context("Klein 9B", "t2i")
        self.assertTrue(packet["resolved"])
        self.assertGreaterEqual(len(packet["compatible_nodes"]), 100)
        self.assertGreaterEqual(len(packet["unknown_nodes"]), 100)
        allowed = {entry["node"] for entry in packet["compatible_nodes"]}
        self.assertIn("UNETLoader", allowed)
        self.assertIn("AuditNode99", allowed)


class ExperienceTest(KnowledgeTestBase):
    def test_experience_summary(self):
        knowledge.record_experience("success", "flux2.klein.9b", "t2i", nodes=["KSampler"])
        knowledge.record_experience("failure", "flux2.klein.9b", "t2i", error_sig="shape mismatch")
        summary = knowledge.experience_summary("flux2.klein.9b", "t2i")
        self.assertEqual(summary["successes"], 1)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["runs"], 2)

    def test_recall_errors_matches_similar_failures_only(self):
        knowledge.record_experience("failure", "flux2.klein.9b", "t2i",
                                    error_sig="mat1 and mat2 shapes cannot be multiplied", nodes=["KSampler"])
        knowledge.record_experience("failure", "flux2.klein.9b", "t2i", error_sig="missing vae file")
        matches = knowledge.recall_errors("mat1 and mat2 shapes cannot be multiplied", 5)
        self.assertTrue(matches)
        self.assertIn("mat1", matches[0]["error"])
        self.assertEqual(len(matches), 1)

    def test_successes_promote_observed_node_to_confirmed(self):
        for _ in range(4):
            knowledge.record_experience("success", "flux2.klein.9b", "t2i", nodes=["VAEDecode"],
                                        pattern_id="klein9b_basic_t2i", config_hash="cfg1")
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "VAEDecode")["state"],
                         knowledge.CONFIRMED_COMPATIBLE)

    def test_promotion_requires_an_exact_pattern(self):
        for _ in range(4):
            knowledge.record_experience("success", "flux2.klein.9b", "t2i", nodes=["VAEDecode"])
        self.assertNotEqual(knowledge.evaluate("flux2.klein.9b", "VAEDecode")["state"],
                            knowledge.CONFIRMED_COMPATIBLE)

    def test_failures_block_promotion(self):
        for _ in range(4):
            knowledge.record_experience("success", "flux2.klein.9b", "t2i", nodes=["VAEDecode"],
                                        pattern_id="klein9b_basic_t2i", config_hash="cfg1")
            knowledge.record_experience("failure", "flux2.klein.9b", "t2i", error_sig="boom",
                                        pattern_id="klein9b_basic_t2i", config_hash="cfg1")
        self.assertNotEqual(knowledge.evaluate("flux2.klein.9b", "VAEDecode")["state"],
                            knowledge.CONFIRMED_COMPATIBLE)

    def test_pattern_trust_promotes_and_user_approval_wins(self):
        for _ in range(4):
            knowledge.record_experience("success", "flux2.klein.9b", "t2i", nodes=["VAEDecode"],
                                        pattern_id="klein9b_basic_t2i", config_hash="cfg1")
        packet = knowledge.get_build_context("Klein 9B", "t2i", ["VAEDecode"])
        pattern = next(item for item in packet["known_good_patterns"] if item["pattern_id"] == "klein9b_basic_t2i")
        self.assertIn(pattern["trust"], ("OBSERVED", "TESTED"))
        result = knowledge.approve_pattern("klein9b_basic_t2i")
        self.assertTrue(result["ok"])
        packet = knowledge.get_build_context("Klein 9B", "t2i", ["VAEDecode"])
        pattern = next(item for item in packet["known_good_patterns"] if item["pattern_id"] == "klein9b_basic_t2i")
        self.assertEqual(pattern["trust"], "USER_APPROVED")


class ResearchTest(KnowledgeTestBase):
    def test_web_claims_stored_low_trust_with_provenance(self):
        result = knowledge.add_claims("flux2.klein.9b", [
            {"key": "latent_format", "value": "flux2", "source_url": "https://example.com/x", "confidence": 0.4},
            {"key": "special", "value": "yes"},
            {"key": "", "value": "ignored"},
        ])
        self.assertEqual(result["stored"], 2)
        fresh = knowledge.freshness()
        self.assertGreaterEqual(fresh["web_properties"], 2)
        self.assertGreaterEqual(fresh["web_evidence"], 2)

    def test_version_change_invalidates_web_knowledge(self):
        knowledge.record_versions({"comfyui": "1.0"})
        knowledge.add_claims("flux2.klein.9b", [{"key": "special", "value": "yes"}])
        self.assertGreaterEqual(knowledge.freshness()["web_properties"], 1)
        result = knowledge.record_versions({"comfyui": "2.0"})
        self.assertIn("comfyui", result["changed"])
        self.assertGreater(result["invalidated"], 0)
        self.assertEqual(knowledge.freshness()["web_properties"], 0)

    def test_web_claims_never_become_confirmed(self):
        knowledge.add_entity("web.model", "model", "Web Model")
        knowledge.add_claims("web.model", [{"key": "latent_format", "value": "flux2"}])
        state = knowledge.evaluate("web.model", "EmptyFlux2LatentImage")["state"]
        self.assertNotEqual(state, knowledge.CONFIRMED_COMPATIBLE)


class StrictCompatibilityTest(KnowledgeTestBase):
    def test_generated_restriction_is_advisory_even_at_high_confidence(self):
        knowledge.add_requirement("node", "Utility", {"key": "latent_format", "value": ["other"]},
                                  confidence=1.0, provenance="llm:test")
        result = knowledge.evaluate("flux2.klein.9b", "Utility")
        self.assertEqual(result["state"], knowledge.UNKNOWN)
        self.assertEqual(result["conflicts"][0]["reason"], "unverified_requirement")
        packet = knowledge.get_build_context("Klein 9B", "t2i", ["Utility"])
        self.assertEqual(packet["exclusions"], [])
        self.assertTrue(packet["compatible_nodes"][0]["warn"])

    def test_weak_rules_do_not_override_documented_generic_node(self):
        for source, confidence in [("llm:test", 1.0), ("web:example", 1.0), ("manifest:weak", 0.1), ("", 1.0)]:
            with self.subTest(source=source):
                knowledge.add_requirement("node", "SaveImage", {"key": "latent_format", "value": ["other"]},
                                          confidence=confidence, provenance=source)
                result = knowledge.evaluate("flux2.klein.9b", "SaveImage")
                self.assertEqual(result["state"], knowledge.GENERIC)
                self.assertTrue(result["conflicts"])

    def test_verified_mismatch_still_blocks_alongside_generated_rules(self):
        knowledge.add_requirement("node", "Utility", {"key": "latent_format", "value": ["flux2"]},
                                  confidence=1.0, provenance="llm:test")
        knowledge.add_requirement("node", "Utility", {"key": "latent_format", "value": ["other"]},
                                  confidence=0.9, provenance="manifest:test")
        result = knowledge.evaluate("flux2.klein.9b", "Utility")
        self.assertEqual(result["state"], knowledge.INFERRED_INCOMPATIBLE)

    def test_absence_of_requirements_is_unknown_not_generic(self):
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "SomeUnmarkedNode")["state"], knowledge.UNKNOWN)

    def test_documented_generic_operation_stays_generic(self):
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "VAEDecode")["state"], knowledge.GENERIC)
        self.assertEqual(knowledge.evaluate("flux2.klein.9b", "SaveImage")["state"], knowledge.GENERIC)

    def test_low_confidence_or_web_claims_cannot_allow(self):
        knowledge.add_entity("lowconf.model", "model", "Low Confidence")
        knowledge.set_property("lowconf.model", "latent_format", "flux2", confidence=0.2, source="web:http://x")
        result = knowledge.evaluate("lowconf.model", "EmptyFlux2LatentImage")
        self.assertEqual(result["state"], knowledge.UNKNOWN)
        self.assertTrue(result["conflicts"])

    def test_confident_claim_yields_inferred(self):
        knowledge.add_entity("conf.model", "model", "Confident")
        knowledge.set_property("conf.model", "latent_format", "flux2", confidence=0.9, source="manifest:doc")
        result = knowledge.evaluate("conf.model", "EmptyFlux2LatentImage")
        self.assertEqual(result["state"], knowledge.INFERRED_COMPATIBLE)
        self.assertGreaterEqual(result["confidence"], 0.9)
        self.assertIn("manifest:doc", result["provenance"])

    def test_partial_contradiction_surfaced_but_match_holds(self):
        knowledge.add_entity("partial.model", "model", "Partial")
        knowledge.set_property("partial.model", "latent_format", "flux2", confidence=0.9, source="a")
        knowledge.set_property("partial.model", "latent_format", "sdxl", confidence=0.9, source="b")
        result = knowledge.evaluate("partial.model", "EmptyFlux2LatentImage")
        self.assertEqual(result["state"], knowledge.INFERRED_COMPATIBLE)
        self.assertTrue(result["conflicts"])

    def test_full_contradiction_degrades_to_unknown(self):
        knowledge.add_entity("full.model", "model", "Full")
        knowledge.set_property("full.model", "latent_format", "sdxl", confidence=0.9, source="a")
        knowledge.set_property("full.model", "latent_format", "wan", confidence=0.9, source="b")
        result = knowledge.evaluate("full.model", "EmptyFlux2LatentImage")
        self.assertEqual(result["state"], knowledge.UNKNOWN)
        self.assertTrue(result["conflicts"])


class EvidenceScopeTest(KnowledgeTestBase):
    def test_evidence_is_scoped_to_the_entity(self):
        knowledge.add_claims("flux2.klein.9b", [{"key": "k1", "value": "v1"}])
        knowledge.add_claims("other.model", [{"key": "k2", "value": "v2", "source_url": "http://x"}])
        packet = knowledge.get_build_context("Klein 9B", "t2i", ["VAEDecode"])
        claims = " ".join(item["claim"] for item in packet["evidence"])
        self.assertIn("flux2.klein.9b:", claims)
        self.assertNotIn("other.model:", claims)

    def test_entity_column_migration_is_idempotent(self):
        conn = knowledge._connect()
        try:
            knowledge._init(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence)")}
            self.assertIn("entity", columns)
            knowledge._init(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence)")}
            self.assertIn("entity", columns)
        finally:
            conn.close()

    def test_legacy_database_migrates_without_error(self):
        knowledge.DB_PATH = os.path.join(self.tmp, "legacy.sqlite")
        conn = sqlite3.connect(knowledge.DB_PATH)
        conn.executescript(
            """
            CREATE TABLE evidence (id INTEGER PRIMARY KEY, claim TEXT, source_type TEXT, source TEXT,
                version TEXT, result TEXT, confidence REAL, trust TEXT, retrieved_at REAL);
            CREATE TABLE experiences (id INTEGER PRIMARY KEY, pattern_id TEXT, model_entity TEXT, task TEXT,
                outcome TEXT, error_sig TEXT, nodes TEXT, source TEXT, created_at REAL);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '1');
            """
        )
        conn.commit()
        conn.close()
        knowledge.index_knowledge()
        conn = knowledge._connect()
        try:
            knowledge._init(conn)
            self.assertIn("entity", {row[1] for row in conn.execute("PRAGMA table_info(evidence)")})
            self.assertIn("config_hash", {row[1] for row in conn.execute("PRAGMA table_info(experiences)")})
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
