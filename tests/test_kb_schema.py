"""Offline tests for KB schema versioning, indexes and scored search."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import patch

NODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NODE_DIR not in sys.path:
    sys.path.insert(0, NODE_DIR)

import kb  # noqa: E402


class KbSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_path = kb.DB_PATH
        kb.DB_PATH = os.path.join(self.tmp, "kb.sqlite")

    def tearDown(self):
        kb.DB_PATH = self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        conn = kb._connect()
        kb._init(conn)
        return conn

    def test_unreadable_changed_doc_preserves_chunks_and_retries(self):
        path = os.path.join(self.tmp, "README.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Node usage\nOriginal documentation for this custom node.")
        with patch.object(kb, "_custom_node_dirs", return_value=[self.tmp]):
            kb.index_local()
            conn = self._conn()
            before = conn.execute("SELECT content FROM chunks WHERE path = ?", (path,)).fetchall()
            self.assertTrue(before)
            conn.close()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# Node usage\nUpdated documentation with additional node usage details.")
            with patch("builtins.open", side_effect=PermissionError("temporarily locked")):
                kb.index_local()
            conn = self._conn()
            self.assertEqual(conn.execute("SELECT content FROM chunks WHERE path = ?", (path,)).fetchall(), before)
            conn.close()
            kb.index_local()
            conn = self._conn()
            after = conn.execute("SELECT content FROM chunks WHERE path = ?", (path,)).fetchall()
            conn.close()
            self.assertNotEqual(after, before)

    def test_schema_version_is_recorded(self):
        conn = self._conn()
        try:
            self.assertEqual(kb._schema_version(conn), kb.SCHEMA_VERSION)
        finally:
            conn.close()

    def test_chunk_indexes_exist(self):
        conn = self._conn()
        try:
            names = {row[1] for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chunks'")}
        finally:
            conn.close()
        self.assertTrue({"chunks_kind", "chunks_pack", "chunks_path"}.issubset(names))

    def test_version_mismatch_rebuilds(self):
        conn = self._conn()
        try:
            conn.execute("INSERT INTO chunks (source_kind, source, content) VALUES ('node', 'A', 'hello world')")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '999')")
            conn.commit()
        finally:
            conn.close()
        conn = self._conn()
        try:
            self.assertEqual(kb._schema_version(conn), kb.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
        finally:
            conn.close()

    def test_search_returns_score_and_rank(self):
        conn = self._conn()
        try:
            conn.execute("INSERT INTO chunks (source_kind, source, pack, content) VALUES ('node', 'KSampler', '', 'hello world sampling node')")
            conn.commit()
        finally:
            conn.close()
        results = kb.search("hello world", 5)
        self.assertTrue(results)
        self.assertIn("score", results[0])
        self.assertGreater(results[0]["score"], 0.0)
        self.assertEqual(results[0]["rank"], 0)

    def test_search_pack_filter(self):
        conn = self._conn()
        try:
            conn.execute("INSERT INTO chunks (source_kind, source, pack, content) VALUES ('pack', 'p1', 'pack-one', 'alpha beta gamma')")
            conn.execute("INSERT INTO chunks (source_kind, source, pack, content) VALUES ('pack', 'p2', 'pack-two', 'alpha beta delta')")
            conn.commit()
        finally:
            conn.close()
        results = kb.search("alpha beta", 5, None, "pack-two")
        self.assertTrue(results)
        self.assertTrue(all(item["pack"] == "pack-two" for item in results))

    def test_retrieve_modes_prefer_expected_sources(self):
        conn = self._conn()
        try:
            conn.execute("INSERT INTO chunks (source_kind, source, pack, title, content) VALUES ('registry', 'PackOne', '', 'PackOne', 'alpha beta')")
            conn.execute("INSERT INTO chunks (source_kind, source, pack, title, content) VALUES ('pack', 'somepack', 'somepack', 'somepack/readme', 'alpha beta')")
            conn.execute("INSERT INTO chunks (source_kind, source, pack, title, content) VALUES ('node', 'AlphaNode', '', 'AlphaNode', 'alpha beta')")
            conn.commit()
        finally:
            conn.close()
        nodes = kb.retrieve("choose_node", "alpha beta", 5)
        self.assertTrue(nodes)
        self.assertEqual(nodes[0]["source_kind"], "node")
        packages = kb.retrieve("find_package", "alpha beta", 5)
        self.assertTrue(packages)
        self.assertTrue(all(item["source_kind"] == "registry" for item in packages))
        fallback = kb.retrieve("unknown_mode", "alpha beta", 5)
        self.assertTrue(fallback)


if __name__ == "__main__":
    unittest.main()


class BuildSkipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_path = kb.DB_PATH
        kb.DB_PATH = os.path.join(self.tmp, "kb.sqlite")

    def tearDown(self):
        kb.DB_PATH = self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _init_db(self):
        conn = kb._connect()
        try:
            kb._init(conn)
        finally:
            conn.close()

    def test_skip_when_built_and_packs_unchanged(self):
        self._init_db()
        kb._meta_set("build_complete", "1")
        kb._meta_set("pack_fingerprint", "fp1")
        with patch.object(kb, "_pack_fingerprint", lambda: "fp1"), \
             patch.object(kb, "_kb_config", lambda: {"rebuild_on_startup": False}):
            self.assertTrue(kb._should_skip_build(False))
            self.assertFalse(kb._should_skip_build(True))
        with patch.object(kb, "_pack_fingerprint", lambda: "fp2"), \
             patch.object(kb, "_kb_config", lambda: {"rebuild_on_startup": False}):
            self.assertFalse(kb._should_skip_build(False))

    def test_do_not_skip_without_completion_marker(self):
        self._init_db()
        with patch.object(kb, "_pack_fingerprint", lambda: "fp1"), \
             patch.object(kb, "_kb_config", lambda: {}):
            self.assertFalse(kb._should_skip_build(False))

    def test_rebuild_on_startup_config_forces_build(self):
        self._init_db()
        kb._meta_set("build_complete", "1")
        kb._meta_set("pack_fingerprint", "fp1")
        with patch.object(kb, "_pack_fingerprint", lambda: "fp1"), \
             patch.object(kb, "_kb_config", lambda: {"rebuild_on_startup": True}):
            self.assertFalse(kb._should_skip_build(False))

    def test_missing_db_is_not_skipped(self):
        with patch.object(kb, "_kb_config", lambda: {}):
            self.assertFalse(kb._should_skip_build(False))
