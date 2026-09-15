"""Offline tests for the shared storage helpers and the memory/KB stores."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

NODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NODE_DIR not in sys.path:
    sys.path.insert(0, NODE_DIR)

import kb  # noqa: E402
import memory  # noqa: E402
import storage  # noqa: E402


class StorageHelpersTest(unittest.TestCase):
    def test_fts_query_tokenizes_and_ors(self):
        self.assertEqual(storage.fts_query("hello world"), '"hello" OR "world"')
        self.assertEqual(storage.fts_query("a bb ccc", min_length=2, limit=2), '"bb" OR "ccc"')
        self.assertEqual(storage.fts_query(""), "")
        self.assertEqual(storage.fts_query("!@#$"), "")

    def test_connect_creates_and_initializes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "test.sqlite")
            conn = storage.connect(path)
            try:
                conn.execute("CREATE TABLE t (x INTEGER)")
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
                self.assertEqual(conn.execute("SELECT x FROM t").fetchone()[0], 1)
            finally:
                conn.close()
            self.assertTrue(os.path.exists(path))


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._original = memory.DB_PATH
        memory.DB_PATH = os.path.join(self.tmp, "memory.sqlite")

    def tearDown(self):
        memory.DB_PATH = self._original

    def test_add_list_search_update_delete(self):
        result = memory.add_lesson("Always use fp8 for SDXL", tags="sdxl", pinned=True)
        self.assertFalse(result["updated"])
        lessons = memory.list_lessons()
        self.assertEqual(len(lessons), 1)
        self.assertNotIn("uses", lessons[0])
        self.assertEqual([lesson["text"] for lesson in memory.search("sdxl")], ["Always use fp8 for SDXL"])
        self.assertEqual([lesson["text"] for lesson in memory.relevant("fp8")], ["Always use fp8 for SDXL"])
        self.assertTrue(memory.update_lesson(result["id"], text="Use fp8 for SDXL v2"))
        self.assertTrue(memory.delete_lesson(result["id"]))
        self.assertEqual(memory.list_lessons(), [])

    def test_add_lesson_deduplicates(self):
        first = memory.add_lesson("Use fp8 checkpoints")
        second = memory.add_lesson("use  FP8   checkpoints")
        self.assertTrue(second["updated"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(memory.list_lessons()), 1)

    def test_empty_lesson_rejected(self):
        with self.assertRaises(ValueError):
            memory.add_lesson("   ")

    def test_empty_update_keeps_existing_lesson(self):
        lesson = memory.add_lesson("Keep this lesson")
        with self.assertRaises(ValueError):
            memory.update_lesson(lesson["id"], text="  ")
        self.assertEqual(memory.list_lessons()[0]["text"], "Keep this lesson")


class KnowledgeBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._original = kb.DB_PATH
        kb.DB_PATH = os.path.join(self.tmp, "kb.sqlite")

    def tearDown(self):
        kb.DB_PATH = self._original

    def test_insert_and_search(self):
        conn = kb._connect()
        try:
            kb._init(conn)
            kb._insert_chunks(
                conn,
                [
                    ("pack", "mypack", "mypack", "/x/readme.md", "", "mypack/readme", "Intro", "Text about samplers and schedulers"),
                    ("node", "KSampler", "", None, "", "KSampler", "Sampling", "KSampler (KSampler)"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        results = kb.search("samplers")
        self.assertEqual([item["title"] for item in results], ["mypack/readme"])
        node = kb.node_docs("KSampler")
        self.assertEqual(node["node_type"], "KSampler")
        self.assertTrue(any(item["source"] == "KSampler" for item in node["results"]))


if __name__ == "__main__":
    unittest.main()
