"""Embedding lifecycle and search regressions. No provider/network access."""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kb


class EmbeddingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.object(kb, "DB_PATH", os.path.join(temp.name, "kb.sqlite"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = kb._connect()
        self.addCleanup(self.conn.close)
        kb._init(self.conn)

    def insert(self, chunk_id, vector, source="pack"):
        self.conn.execute("INSERT INTO chunks(id, source_kind, source, content) VALUES (?, ?, ?, ?)",
                          (chunk_id, source, str(chunk_id), "text"))
        self.conn.execute("INSERT INTO embeddings VALUES (?, ?, ?)",
                          (chunk_id, len(vector), np.asarray(vector, dtype="float32").tobytes()))
        self.conn.commit()

    def test_deleted_chunk_cannot_leave_vector_for_reused_id(self):
        self.insert(1, [1, 0])
        self.conn.execute("DELETE FROM chunks WHERE id=1")
        self.conn.execute("INSERT INTO chunks(id, source_kind, source, content) VALUES (1, 'pack', 'new', 'new')")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)

    def test_existing_database_vectors_invalidated_only_once(self):
        self.insert(1, [1, 0])
        self.conn.execute("DELETE FROM meta WHERE key='embeddings_cleanup_v1'")
        self.conn.commit()
        kb._init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)
        self.conn.execute("INSERT INTO embeddings VALUES (1, 2, ?)", (np.array([0, 1], dtype="float32").tobytes(),))
        self.conn.commit()
        kb._init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 1)

    def test_same_count_vector_replacement_refreshes_cache(self):
        self.insert(1, [1, 0])
        _, matrix = kb._load_vectors(self.conn)
        np.testing.assert_allclose(matrix, [[1, 0]])
        self.conn.execute("UPDATE embeddings SET vec=? WHERE chunk_id=1", (np.array([0, 1], dtype="float32").tobytes(),))
        self.conn.commit()
        _, matrix = kb._load_vectors(self.conn)
        np.testing.assert_allclose(matrix, [[0, 1]])

    def test_search_uses_stored_model_and_finds_filtered_sources(self):
        for chunk_id in range(1, 26):
            self.insert(chunk_id, [1, 0])
        self.insert(26, [0, 1], source="node")
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'stored-model')")
        self.conn.commit()
        provider = SimpleNamespace(embed=AsyncMock(return_value=[[1, 0]]))
        with patch.object(kb, "_embed_settings", return_value=({}, "")), patch.object(kb, "_providers_module", return_value=provider):
            results = kb._vector_search(self.conn, "query", 1, "node")
        self.assertEqual([chunk_id for chunk_id, _ in results], [26])
        provider.embed.assert_awaited_once_with({}, ["query"], "stored-model")

    def test_changed_model_skips_incompatible_vectors(self):
        self.insert(1, [1, 0])
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'old-model')")
        self.conn.commit()
        provider = SimpleNamespace(embed=AsyncMock())
        with patch.object(kb, "_embed_settings", return_value=({}, "new-model")), patch.object(kb, "_providers_module", return_value=provider):
            self.assertEqual(kb._vector_search(self.conn, "query", 1, None), [])
        provider.embed.assert_not_awaited()
