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

    def test_init_preserves_existing_database_vectors_without_cleanup_marker(self):
        self.insert(1, [1, 0])
        self.conn.execute("DELETE FROM meta WHERE key='embeddings_cleanup_v1'")
        self.conn.commit()
        kb._init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT vec FROM embeddings WHERE chunk_id=1").fetchone()[0],
                         np.array([1, 0], dtype="float32").tobytes())
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

    def test_tail_fragments_are_searchable_and_indexing_resumes(self):
        content = "a" * 18 + "TAIL"
        self.conn.execute("INSERT INTO chunks(id, source_kind, source, content) VALUES (1, 'node', 'A', ?)", (content,))
        self.conn.commit()
        async def embed(config, texts, model):
            return [[0, 1] if "TAIL" in text else [1, 0] for text in texts]
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(10, 36, "model")),
                                   embed=AsyncMock(side_effect=embed))
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            self.assertEqual(kb.index_embeddings(), 1)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embedding_fragments").fetchone()[0], 2)
            self.assertEqual("".join(provider.embed.await_args.args[1]), content)
            provider.embed.reset_mock()
            self.assertEqual(kb.index_embeddings(), 1)
            provider.embed.assert_not_awaited()
            self.insert(2, [0.7, 0.7])
            results = kb._vector_search(self.conn, "TAIL", 0, None)
            self.assertEqual([row[0] for row in results], [1, 2])
            # The query tail is sent too, instead of being sliced off.
            kb._vector_search(self.conn, content, 0, None)
            self.assertEqual("".join(provider.embed.await_args.args[1]), content)
        self.conn.execute("DELETE FROM chunks WHERE id=1")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embedding_fragments").fetchone()[0], 0)

    def test_fragment_updates_invalidate_cached_vectors(self):
        self.insert(1, [1, 0])
        kb._load_vectors(self.conn)
        self.conn.execute("INSERT INTO embedding_fragments VALUES (1, 1, 2, ?)", (np.array([0, 1], dtype="float32").tobytes(),))
        self.conn.commit()
        ids, matrix = kb._load_vectors(self.conn)
        self.assertEqual(ids.tolist(), [1, 1])
        np.testing.assert_allclose(matrix, [[1, 0], [0, 1]])

    def test_legacy_vectors_survive_window_detection_without_reembedding(self):
        self.insert(1, [1, 0])
        self.conn.execute("INSERT INTO meta VALUES ('embed_model', 'model')")
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(100, 360, "model")),
                                   embed=AsyncMock(return_value=[[0, 1]]))
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider):
            kb.index_embeddings()
            kb.index_embeddings()
        provider.embed.assert_not_awaited()
        self.assertEqual(self.conn.execute("SELECT content FROM chunks WHERE id=1").fetchone()[0], "text")
        self.assertEqual(self.conn.execute("SELECT vec FROM embeddings WHERE chunk_id=1").fetchone()[0],
                         np.array([1, 0], dtype="float32").tobytes())
        _, matrix = kb._load_vectors(self.conn)
        np.testing.assert_allclose(matrix, [[1, 0]])

    def test_interrupted_fragments_do_not_mark_chunk_complete(self):
        self.conn.execute("INSERT INTO chunks(source_kind, source, content) VALUES ('node', 'A', ?)", ("a" * 30,))
        self.conn.commit()
        provider = SimpleNamespace(embedding_limits=AsyncMock(return_value=(10, 36, "model")),
                                   embed=AsyncMock(side_effect=[[[1, 0]], RuntimeError("offline")]))
        with patch.object(kb, "_embed_settings", return_value=({}, "model")), \
                patch.object(kb, "_resolve_embed_model", return_value="model"), \
                patch.object(kb, "_providers_module", return_value=provider), patch.object(kb, "EMBED_BATCH", 1):
            kb.index_embeddings()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embedding_fragments").fetchone()[0], 0)
        self.assertEqual(kb._state["embed_error"], "offline")
