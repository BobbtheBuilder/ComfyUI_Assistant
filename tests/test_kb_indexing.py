"""Offline tests for the KB indexer helpers (node schema text + workflow summaries)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
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


class ChunkBudgetTest(unittest.TestCase):
    def test_long_content_splits_into_bounded_chunks(self):
        text = "enum(" + ", ".join(f"value_{index}" for index in range(4000)) + ")"
        pieces = kb._chunks_for_text(text, 500, 50)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= 500 for _heading, piece in pieces))

    def test_effective_chunk_chars_uses_the_setting(self):
        def fake_limit(name, default=0):
            if name == "kb_chunk_chars":
                return 400
            if name == "kb_chunk_overlap":
                return 60
            return default

        with patch.object(kb, "_limit", side_effect=fake_limit):
            self.assertEqual(kb._effective_chunk_chars(), (400, 60))


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

    def test_schema_changes_with_same_node_name_change_fingerprint(self):
        with patch.object(kb, "_custom_node_dirs", return_value=[]):
            before = kb._build_fingerprint({"A": {"description": "old"}}, (1200, 150))
            after = kb._build_fingerprint({"A": {"description": "new"}}, (1200, 150))
        self.assertNotEqual(before, after)

    def test_doc_edit_and_chunk_setting_change_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(kb, "_custom_node_dirs", return_value=[tmp]):
            path = Path(tmp) / "README.md"
            path.write_text("first")
            before = kb._build_fingerprint({}, (1200, 150))
            path.write_text("a changed document")
            after = kb._build_fingerprint({}, (1200, 150))
            different_chunking = kb._build_fingerprint({}, (600, 150))
        self.assertNotEqual(before, after)
        self.assertNotEqual(after, different_chunking)

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
                patch.object(kb, "_collect_node_records", return_value={}), \
                patch.object(kb, "_effective_chunk_chars", return_value=(1200, 150)), \
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
        mocks["index_embeddings"].assert_called_once()
        mocks["meta_set"].assert_not_called()
        self.assertEqual(kb._state["phase"], "Ready")
        self.assertFalse(kb._state["building"])

    def test_missing_fingerprint_runs_full_build(self):
        mocks = self._run("")
        mocks["index_local"].assert_called_once()
        mocks["index_node_schemas"].assert_called_once()
        mocks["index_embeddings"].assert_called_once()
        mocks["meta_set"].assert_any_call("build_fingerprint", "FP")

    def test_force_rebuilds_even_when_fingerprint_matches(self):
        mocks = self._run("FP", force=True)
        mocks["index_local"].assert_called_once()
        mocks["index_node_schemas"].assert_called_once()

    def test_records_fingerprint_even_if_embeddings_fail(self):
        mocks = self._run("", embeddings_error=True)
        mocks["meta_set"].assert_any_call("build_fingerprint", "FP")
        self.assertIn("embeddings", kb._state["error"])


class IncrementalIndexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(kb, "DB_PATH", os.path.join(tmp.name, "kb.sqlite"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = kb._connect()
        kb._init(self.conn)
        self.addCleanup(self.conn.close)

    def info(self, description="first"):
        return {"name": "A", "input": {}, "output": [], "description": description}

    def test_unchanged_nodes_keep_ids_and_vectors_changed_nodes_invalidate(self):
        records = {"A": self.info()}
        kb.index_node_schemas(records, (1200, 150))
        row = self.conn.execute("SELECT id, content FROM chunks").fetchone()
        self.conn.execute("INSERT INTO embeddings VALUES (?, 1, ?)", (row[0], b"vector"))
        self.conn.commit()
        kb.index_node_schemas(records, (1200, 150))
        self.assertEqual(self.conn.execute("SELECT id, content FROM chunks").fetchone(), row)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 1)
        kb.index_node_schemas({"A": self.info("changed")}, (1200, 150))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)
        self.assertIn("changed", self.conn.execute("SELECT content FROM chunks").fetchone()[0])

    def test_absent_records_are_retained_but_broken_schemas_lose_stale_chunks(self):
        kb.index_node_schemas({"A": self.info()}, (1200, 150))
        kb.index_node_schemas({}, (1200, 150))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 1)
        kb.index_node_schemas({"A": {"name": "A", "schema_error": "broken"}}, (1200, 150))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM node_records").fetchone()[0], 1)

    def test_reconcile_handles_identical_duplicate_chunks(self):
        record = ("example", "a", "", None, "", "", "", "same")
        kb._insert_chunks(self.conn, [record, record])
        before = self.conn.execute("SELECT id FROM chunks").fetchall()
        kb._sync_chunks(self.conn, [record, record], "source_kind='example'")
        self.assertEqual(self.conn.execute("SELECT id FROM chunks").fetchall(), before)
        kb._sync_chunks(self.conn, [record], "source_kind='example'")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 1)

    def test_provider_outage_preserves_previous_auto_chunk_size(self):
        kb.index_node_schemas({"A": self.info()}, (7000, 150))
        with patch.object(kb, "_embed_settings", return_value=(None, "")), \
                patch.object(kb, "_limit", side_effect=lambda name, default=0: 150 if name == "kb_chunk_overlap" else default):
            self.assertEqual(kb._effective_chunk_chars(), (7000, 150))

    def test_detected_window_changes_preserve_vectors_and_input_policy(self):
        kb._insert_chunks(self.conn, [("node", "A", "", None, "", "", "", "same")])
        chunk_id = self.conn.execute("SELECT id FROM chunks").fetchone()[0]
        self.conn.execute("INSERT INTO embeddings VALUES (?, 1, ?)", (chunk_id, b"vector"))
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'model')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_identity', 'server|model')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_input_policy', '[\"full-text-v1\",2048]')")
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(side_effect=[(2048, 8000, "detected"),
                                                                            (512, 1800, "fallback"),
                                                                            (2048, 8000, "detected")]),
                                   embedding_identity=lambda config, model: "server|model",
                                   embed=AsyncMock())
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            for _ in range(3):
                kb.index_embeddings()
        self.assertEqual(self.conn.execute("SELECT vec FROM embeddings").fetchone()[0], b"vector")
        provider.embed.assert_not_awaited()

    def test_detected_window_changes_keep_stored_chunk_boundaries(self):
        self.conn.execute("INSERT INTO meta VALUES ('node_chunking', '[7000,150]')")
        self.conn.commit()
        limits = SimpleNamespace(embedding_limits=AsyncMock(return_value=(512, 1800, "detected")))
        with patch.object(kb, "_limit", side_effect=lambda name, default=0: 150 if name == "kb_chunk_overlap" else default), \
                patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=limits):
            self.assertEqual(kb._effective_chunk_chars(), (7000, 150))

    def test_fallback_window_keeps_last_verified_value_for_same_identity(self):
        text = "x" * 1000
        kb._insert_chunks(self.conn, [("node", "A", "", None, "", "", "", text)])
        self.conn.execute("INSERT INTO meta VALUES ('embed_window_verified_identity', 'server|model')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_window_verified_tokens', '2048')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_window_tokens', '2048')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_window_source', 'detected')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'model')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_identity', 'server|model')")
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(512, 1800, "fallback")),
                                   embedding_identity=lambda config, model: "server|model",
                                   embed=AsyncMock(return_value=[[1.0]]))
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            kb.index_embeddings()
        self.assertEqual(provider.embed.await_args.args[1], [text])
        self.assertEqual(kb._meta_get("embed_window_tokens"), "2048")
        self.assertEqual(kb._meta_get("embed_window_verified_tokens"), "2048")

    def test_init_does_not_delete_vectors_for_historical_cleanup_marker(self):
        kb._insert_chunks(self.conn, [("node", "A", "", None, "", "", "", "same")])
        chunk_id = self.conn.execute("SELECT id FROM chunks").fetchone()[0]
        self.conn.execute("INSERT INTO embeddings VALUES (?, 1, ?)", (chunk_id, b"old-vector"))
        self.conn.commit()
        kb._init(self.conn)
        self.assertEqual(self.conn.execute("SELECT vec FROM embeddings").fetchone()[0], b"old-vector")

    def test_endpoint_change_preserves_vectors_until_explicit_compatibility_choice(self):
        kb._insert_chunks(self.conn, [("node", "A", "", None, "", "", "", "same")])
        chunk_id = self.conn.execute("SELECT id FROM chunks").fetchone()[0]
        self.conn.execute("INSERT INTO embeddings VALUES (?, 1, ?)", (chunk_id, b"vector"))
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'model')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_identity', 'old-server|model')")
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(512, 1800, "fallback")),
                                   embedding_identity=lambda config, model: "new-server|model", embed=AsyncMock())
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            kb.index_embeddings()
        self.assertEqual(self.conn.execute("SELECT vec FROM embeddings").fetchone()[0], b"vector")
        self.assertTrue(kb._state["embed_endpoint_confirmation_required"])
        self.assertFalse(kb.confirm_embedding_compatibility("stale", "new-server|model", "model",
                                                             "new-server|model", "reuse"))
        self.assertFalse(kb.confirm_embedding_compatibility("old-server|model", "new-server|different-model",
                                                             "different-model", "new-server|different-model", "reuse"))
        self.assertTrue(kb.confirm_embedding_compatibility("old-server|model", "new-server|model", "model",
                                                            "new-server|model", "reuse"))
        self.assertEqual(kb._meta_get("embed_identity"), "new-server|model")
        self.assertEqual(self.conn.execute("SELECT vec FROM embeddings").fetchone()[0], b"vector")
        provider.embed.assert_not_awaited()

    def test_endpoint_change_with_no_vectors_adopts_endpoint_and_indexes_pending_chunk(self):
        kb._insert_chunks(self.conn, [("node", "A", "", None, "", "", "", "pending")])
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'model')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_identity', 'old-server|model')")
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(512, 1800, "fallback")),
                                   embedding_identity=lambda config, model: "new-server|model",
                                   embed=AsyncMock(return_value=[[2.0]]))
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            kb.index_embeddings()
        self.assertEqual(kb._meta_get("embed_identity"), "new-server|model")
        self.assertFalse(kb._state["embed_endpoint_confirmation_required"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 1)

    def test_new_model_provider_failure_keeps_old_vectors_and_identity(self):
        kb._insert_chunks(self.conn, [("node", "A", "", None, "", "", "", "same")])
        chunk_id = self.conn.execute("SELECT id FROM chunks").fetchone()[0]
        self.conn.execute("INSERT INTO embeddings VALUES (?, 1, ?)", (chunk_id, b"old-vector"))
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'old-model')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_identity', 'server|old-model')")
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(512, 1800, "fallback")),
                                   embedding_identity=lambda config, model: "server|new-model",
                                   embed=AsyncMock(side_effect=RuntimeError("provider offline")))
        with patch.object(kb, "_embed_settings", return_value=({}, "new-model")), \
                patch.object(kb, "_resolve_embed_model", return_value="new-model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            kb.index_embeddings()
        self.assertEqual(self.conn.execute("SELECT vec FROM embeddings").fetchone()[0], b"old-vector")
        self.assertEqual(kb._meta_get("embed_model"), "old-model")
        self.assertEqual(kb._meta_get("embed_identity"), "server|old-model")

    def test_new_model_replaces_old_vectors_only_after_first_batch_succeeds(self):
        kb._insert_chunks(self.conn, [("node", "A", "", None, "", "", "", "first"),
                                     ("node", "B", "", None, "", "", "", "second")])
        chunk_ids = [row[0] for row in self.conn.execute("SELECT id FROM chunks ORDER BY id")]
        self.conn.execute("INSERT INTO embeddings VALUES (?, 1, ?)", (chunk_ids[0], b"old-vector"))
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'old-model')")
        self.conn.execute("INSERT INTO meta VALUES ('embed_identity', 'server|old-model')")
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(512, 1800, "fallback")),
                                   embedding_identity=lambda config, model: "server|new-model",
                                   embed=AsyncMock(return_value=[[3.0], [4.0]]))
        with patch.object(kb, "_embed_settings", return_value=({}, "new-model")), \
                patch.object(kb, "_resolve_embed_model", return_value="new-model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            kb.index_embeddings()
        self.assertEqual(kb._meta_get("embed_model"), "new-model")
        self.assertEqual(kb._meta_get("embed_identity"), "server|new-model")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 2)
        self.assertNotIn(b"old-vector", [row[0] for row in self.conn.execute("SELECT vec FROM embeddings")])

    def test_graft_generated_tree_is_ignored_by_fingerprint_and_reconciled(self):
        with tempfile.TemporaryDirectory() as root:
            graft_dir = os.path.join(root, "graft")
            os.makedirs(graft_dir)
            marker = os.path.join(graft_dir, "INDEX.md")
            generated = os.path.join(graft_dir, "kb.md")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("# graft — repo map\n")
            with open(generated, "w", encoding="utf-8") as handle:
                handle.write("derived graph")
            kb._insert_chunks(self.conn, [("pack", "pkg", "pkg", generated, "", "graft/kb.md", "", "derived")])
            self.conn.execute("INSERT INTO files VALUES (?, 1, 10)", (generated,))
            self.conn.commit()
            with patch.object(kb, "_custom_node_dirs", return_value=[root]), \
                    patch.object(kb, "_manager_dir", return_value=""), \
                    patch.object(kb, "_kb_config", return_value={}):
                first = kb._build_fingerprint()
                with open(generated, "w", encoding="utf-8") as handle:
                    handle.write("new derived graph text")
                self.assertEqual(kb._build_fingerprint(), first)
                kb.index_local()
            self.assertTrue(os.path.isfile(generated))
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chunks WHERE path = ?", (generated,)).fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM files WHERE path = ?", (generated,)).fetchone()[0], 0)

    def test_live_counts_refresh_while_building(self):
        kb.index_node_schemas({"A": self.info()}, (1200, 150))
        with patch.dict(kb._state, {"building": True, "chunks": 0}), patch.object(kb, "node_coverage", return_value={}):
            self.assertEqual(kb.status()["chunks"], 1)

    def test_snapshot_is_passed_to_every_record(self):
        inventory = ({"A": object(), "B": object()}, {})
        with patch.object(kb.node_catalog, "snapshot", return_value=inventory), \
                patch.object(kb.node_catalog, "record", side_effect=lambda name, inventory: {"name": name}) as record:
            result = kb._collect_node_records()
        self.assertEqual(set(result), {"A", "B"})
        self.assertTrue(all(call.kwargs["inventory"] is inventory for call in record.call_args_list))

    def test_embedding_interruption_resumes_committed_batches(self):
        records = [("node", str(i), "", None, "", "", "", str(i)) for i in range(3)]
        kb._insert_chunks(self.conn, records)
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(100, 400, "model")),
                                   embed=AsyncMock(side_effect=[[[1.0], [2.0]], RuntimeError("offline")]))
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider), patch.object(kb, "EMBED_BATCH", 2):
            kb.index_embeddings()
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 2)
            self.assertEqual(kb._meta_get("embed_model"), "model")
            provider.embed = AsyncMock(return_value=[[3.0]])
            kb.index_embeddings()
        provider.embed.assert_awaited_once_with({}, ["2"], "model")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 3)
        self.assertEqual(kb._state["embed_error"], "")

    def test_incomplete_embedding_batch_is_not_committed(self):
        kb._insert_chunks(self.conn, [("node", "A", "", None, "", "", "", "a")])
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(100, 400, "model")),
                                   embed=AsyncMock(return_value=[]))
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            kb.index_embeddings()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)
        self.assertIn("incomplete", kb._state["embed_error"])
        self.assertEqual(kb._state["progress_done"], 0)

    def test_two_builds_reuse_database_and_only_embed_new_nodes(self):
        records = {"A": self.info()}
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(100, 400, "model")),
                                   embed=AsyncMock(side_effect=lambda config, texts, model: [[1.0] for text in texts]))
        with patch.object(kb, "_collect_node_records", side_effect=lambda: dict(records)), \
                patch.object(kb, "_custom_node_dirs", return_value=[]), \
                patch.object(kb, "_manager_dir", return_value=""), \
                patch.object(kb, "_kb_config", return_value={"examples": False, "registry": False, "extended_official": False}), \
                patch.object(kb, "_effective_chunk_chars", return_value=(1200, 150)), \
                patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider), \
                patch.object(kb, "compact"), patch.object(kb, "_debug_log") as log, \
                patch.object(kb, "index_node_schemas", wraps=kb.index_node_schemas) as index_nodes:
            kb.build(auto_official=False)
            first = self.conn.execute("SELECT id, content FROM chunks").fetchall()
            self.assertEqual(kb._state["error"], "")
            kb.build(auto_official=False)
            self.assertEqual(self.conn.execute("SELECT id, content FROM chunks").fetchall(), first)
            self.assertEqual(index_nodes.call_count, 1)
            self.assertEqual(provider.embed.await_count, 1)
            records["B"] = {**self.info(), "name": "B"}
            kb.build(auto_official=False)
            self.assertEqual(index_nodes.call_count, 2)
            self.assertEqual(provider.embed.await_count, 2)
            self.assertEqual(len(provider.embed.await_args.args[1]), 1)
            self.assertTrue(any(call.args[1] == "build.skipped" for call in log.call_args_list))

    def test_official_refresh_preserves_extended_docs_and_vectors(self):
        record = ("official", "ext:comfyui-readme", "", None, "ext:comfyui-readme", "readme", "", "keep")
        kb._insert_chunks(self.conn, [record])
        self.conn.commit()
        kb._index_official_text("# Main page\nSource: https://example.test\nContent")
        self.assertEqual(self.conn.execute("SELECT content FROM chunks WHERE source LIKE 'ext:%'").fetchone()[0], "keep")
        with patch.object(kb, "_fetch_url_text", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                kb.index_extended_official()
        self.assertEqual(self.conn.execute("SELECT content FROM chunks WHERE source LIKE 'ext:%'").fetchone()[0], "keep")

    def test_startup_does_not_schedule_kb_build(self):
        source = Path(kb.__file__).with_name("__init__.py").read_text()
        tree = ast.parse(source)
        # Import/startup must leave the expensive KB scan lazy until a user opens Assistant.
        self.assertFalse(any(isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_kb" for node in tree.body))
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                             and node.func.attr == "append" for node in tree.body))


if __name__ == "__main__":
    unittest.main()
