from __future__ import annotations

import difflib
import os
import re
import sqlite3
import threading
import time
from typing import Any

try:
    from .storage import connect, fts_query
except ImportError:
    from storage import connect, fts_query

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(NODE_DIR, "assistant_memory.sqlite")

_lock = threading.RLock()

# A near-duplicate save merges into the existing lesson instead of adding another row.
SIMILARITY_MERGE = 0.60
COSINE_MERGE = 0.90
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_STOPWORDS = frozenset(
    "a an and any are as at be by do does for from in into is it its no not of on or that the "
    "them they this to unless use used user using was were with workflow workflows node nodes".split()
)
_vectors_cache: dict[str, Any] = {"key": None, "count": -1, "ids": None, "matrix": None}


def _connect() -> sqlite3.Connection:
    return connect(DB_PATH)


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            tags TEXT,
            source TEXT,
            created_at REAL,
            updated_at REAL,
            enabled INTEGER DEFAULT 1,
            pinned INTEGER DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(
            text, tags, content='lessons', content_rowid='id', tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS lessons_ai AFTER INSERT ON lessons BEGIN
            INSERT INTO lessons_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS lessons_ad AFTER DELETE ON lessons BEGIN
            INSERT INTO lessons_fts(lessons_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS lessons_au AFTER UPDATE ON lessons BEGIN
            INSERT INTO lessons_fts(lessons_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
            INSERT INTO lessons_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
        END;
        CREATE TABLE IF NOT EXISTS lesson_vectors (
            lesson_id INTEGER PRIMARY KEY,
            dim INTEGER,
            vec BLOB
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TRIGGER IF NOT EXISTS lessons_vectors_ad AFTER DELETE ON lessons BEGIN
            DELETE FROM lesson_vectors WHERE lesson_id = old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS lesson_vectors_ai AFTER INSERT ON lesson_vectors BEGIN
            INSERT INTO meta(key, value) VALUES ('lesson_vectors_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS lesson_vectors_au AFTER UPDATE ON lesson_vectors BEGIN
            INSERT INTO meta(key, value) VALUES ('lesson_vectors_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS lesson_vectors_ade AFTER DELETE ON lesson_vectors BEGIN
            INSERT INTO meta(key, value) VALUES ('lesson_vectors_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        """
    )


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall((text or "").lower()) if token not in _STOPWORDS}


def _similarity(a: str, b: str) -> float:
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    shared = len(left & right)
    dice = 2 * shared / (len(left) + len(right))
    jaccard = shared / len(left | right)
    containment = shared / min(len(left), len(right))
    ratio = difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()
    return max(dice, jaccard, containment * 0.9, ratio)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "text": row[1],
        "tags": row[2] or "",
        "source": row[3] or "",
        "created_at": row[4],
        "updated_at": row[5],
        "enabled": bool(row[6]),
        "pinned": bool(row[7]),
    }


_COLUMNS = "id, text, tags, source, created_at, updated_at, enabled, pinned"
_SELECT_ALL = "SELECT " + _COLUMNS + " FROM lessons"
_SELECT_ENABLED = _SELECT_ALL + " WHERE enabled = 1"
_SELECT_ALL_ORDERED = _SELECT_ALL + " ORDER BY pinned DESC, updated_at DESC"
_SELECT_ENABLED_ORDERED = _SELECT_ENABLED + " ORDER BY pinned DESC, updated_at DESC"
_SELECT_ENABLED_PINNED = _SELECT_ENABLED + " AND pinned = 1 ORDER BY updated_at DESC"
_SELECT_ENABLED_LIMIT = _SELECT_ENABLED + " ORDER BY updated_at DESC LIMIT ?"
_SELECT_BY_ID = _SELECT_ALL + " WHERE id = ?"
_SEARCH_SQL = (
    "SELECT " + ", ".join("l." + column for column in _COLUMNS.split(", "))
    + " FROM lessons_fts JOIN lessons l ON l.id = lessons_fts.rowid "
    "WHERE lessons_fts MATCH ? AND l.enabled = 1 ORDER BY bm25(lessons_fts)"
)
_UPDATE_SQL = (
    "UPDATE lessons SET text = COALESCE(?, text), tags = COALESCE(?, tags), "
    "enabled = COALESCE(?, enabled), pinned = COALESCE(?, pinned), updated_at = ? WHERE id = ?"
)


def _merge_into(conn: sqlite3.Connection, existing: dict[str, Any], tags: str, pinned: bool, now: float) -> None:
    conn.execute(
        "UPDATE lessons SET tags = ?, pinned = ?, updated_at = ? WHERE id = ?",
        (tags or existing["tags"], 1 if pinned else existing["pinned"], now, existing["id"]),
    )


def _store_vector(conn: sqlite3.Connection, lesson_id: int, vector: Any) -> None:
    import numpy as np

    array = np.asarray(vector, dtype="float32")
    if array.size == 0:
        return
    conn.execute(
        "INSERT OR REPLACE INTO lesson_vectors (lesson_id, dim, vec) VALUES (?, ?, ?)",
        (int(lesson_id), int(array.shape[0]), array.tobytes()),
    )
    with _lock:
        _vectors_cache["count"] = -1


def stored_embed_model() -> str:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute("SELECT value FROM meta WHERE key = 'lesson_embed_model'").fetchone()
            return row[0] if row else ""
        finally:
            conn.close()


def ensure_embed_model(model: str) -> None:
    """Drop stored vectors when the embedding model changes (different vector space)."""
    model = str(model or "")
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute("SELECT value FROM meta WHERE key = 'lesson_embed_model'").fetchone()
            if (row[0] if row else "") != model:
                conn.execute("DELETE FROM lesson_vectors")
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('lesson_embed_model', ?)", (model,))
                conn.commit()
                _vectors_cache.update({"key": None, "count": -1, "ids": None, "matrix": None})
        finally:
            conn.close()


def set_embed_error(message: str) -> None:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('lesson_embed_error', ?)", (str(message or ""),)
            )
            conn.commit()
        finally:
            conn.close()


def set_vector(lesson_id: int, vector: Any) -> bool:
    if vector is None:
        return False
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            _store_vector(conn, int(lesson_id), vector)
            conn.commit()
            return True
        finally:
            conn.close()


def pending_lessons() -> list[tuple[int, str]]:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            rows = conn.execute(
                "SELECT l.id, l.text FROM lessons l LEFT JOIN lesson_vectors v ON v.lesson_id = l.id "
                "WHERE l.enabled = 1 AND v.lesson_id IS NULL ORDER BY l.updated_at DESC"
            ).fetchall()
            return [(int(row[0]), str(row[1])) for row in rows]
        finally:
            conn.close()


def vector_stats() -> dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            lessons = conn.execute("SELECT COUNT(*) FROM lessons WHERE enabled = 1").fetchone()[0]
            embedded = conn.execute(
                "SELECT COUNT(*) FROM lesson_vectors v JOIN lessons l ON l.id = v.lesson_id WHERE l.enabled = 1"
            ).fetchone()[0]
            model = conn.execute("SELECT value FROM meta WHERE key = 'lesson_embed_model'").fetchone()
            error = conn.execute("SELECT value FROM meta WHERE key = 'lesson_embed_error'").fetchone()
            return {
                "lessons": int(lessons),
                "embedded": int(embedded),
                "model": model[0] if model else "",
                "error": error[0] if error else "",
            }
        finally:
            conn.close()


def _load_vectors(conn: sqlite3.Connection) -> tuple[Any, Any]:
    import numpy as np

    count = conn.execute("SELECT COUNT(*) FROM lesson_vectors").fetchone()[0]
    revision = conn.execute("SELECT value FROM meta WHERE key = 'lesson_vectors_revision'").fetchone()
    key = (os.path.realpath(DB_PATH), revision[0] if revision else "0")
    with _lock:
        if (_vectors_cache.get("key") == key and _vectors_cache.get("count") == count
                and _vectors_cache.get("matrix") is not None):
            return _vectors_cache["ids"], _vectors_cache["matrix"]
    ids: list[int] = []
    vectors = []
    for lesson_id, _dim, blob in conn.execute("SELECT lesson_id, dim, vec FROM lesson_vectors"):
        ids.append(int(lesson_id))
        vectors.append(np.frombuffer(blob, dtype="float32"))
    if not vectors or len({vector.shape[0] for vector in vectors}) != 1:
        with _lock:
            _vectors_cache.update({"key": key, "count": count, "ids": None, "matrix": None})
        return None, None
    matrix = np.vstack(vectors)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    id_array = np.asarray(ids, dtype="int64")
    with _lock:
        _vectors_cache.update({"key": key, "count": count, "ids": id_array, "matrix": matrix})
    return id_array, matrix


def _cosine_rank(conn: sqlite3.Connection, vector: Any, limit: int = 5) -> list[tuple[int, float]]:
    if vector is None:
        return []
    import numpy as np

    ids, matrix = _load_vectors(conn)
    if matrix is None:
        return []
    query = np.asarray(vector, dtype="float32")
    if query.shape[0] != matrix.shape[1]:
        return []
    query = query / (np.linalg.norm(query) + 1e-9)
    scores = matrix @ query
    order = np.argsort(-scores)
    if limit and limit > 0:
        order = order[: int(limit)]
    return [(int(ids[position]), float(scores[position])) for position in order]


def _vector_results(conn: sqlite3.Connection, query_vec: Any, limit: int) -> list[tuple[int, dict[str, Any]]]:
    results: list[tuple[int, dict[str, Any]]] = []
    for lesson_id, _score in _cosine_rank(conn, query_vec, limit):
        row = conn.execute(_SELECT_BY_ID, (lesson_id,)).fetchone()
        if not row:
            continue
        item = _row_to_dict(row)
        if not item["enabled"]:
            continue
        results.append((lesson_id, item))
    return results


def _fuse(ranked_lists: list[list[tuple[int, dict[str, Any]]]], limit: int) -> list[dict[str, Any]]:
    scores: dict[int, float] = {}
    items: dict[int, dict[str, Any]] = {}
    for ranked in ranked_lists:
        for rank, (lesson_id, item) in enumerate(ranked):
            scores[lesson_id] = scores.get(lesson_id, 0.0) + 1.0 / (60 + rank)
            items.setdefault(lesson_id, item)
    ordered = sorted(scores, key=lambda lesson_id: -scores[lesson_id])
    if limit and limit > 0:
        ordered = ordered[:limit]
    return [items[lesson_id] for lesson_id in ordered]


def _wanted(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def list_lessons(enabled_only: bool = False) -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            query = _SELECT_ENABLED_ORDERED if enabled_only else _SELECT_ALL_ORDERED
            return [_row_to_dict(row) for row in conn.execute(query).fetchall()]
        finally:
            conn.close()


def add_lesson(
    text: str,
    tags: str = "",
    source: str = "user",
    pinned: bool = False,
    vector: Any = None,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Lesson text is required.")
    tags = (tags or "").strip()
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            normalized = _normalize(text)
            for row in conn.execute(_SELECT_ENABLED).fetchall():
                existing = _row_to_dict(row)
                if _normalize(existing["text"]) == normalized:
                    _merge_into(conn, existing, tags, pinned, now)
                    conn.commit()
                    return {"id": existing["id"], "updated": True, "merged": True, "similarity": 1.0}
            cosine = {lesson_id: score for lesson_id, score in _cosine_rank(conn, vector, limit=5)}
            rule_best: dict[str, Any] | None = None
            rule_score = 0.0
            cosine_best: dict[str, Any] | None = None
            cosine_score = 0.0
            for row in conn.execute(_SELECT_ENABLED).fetchall():
                existing = _row_to_dict(row)
                score = _similarity(existing["text"], text)
                if score > rule_score:
                    rule_score, rule_best = score, existing
                closeness = cosine.get(existing["id"], 0.0)
                if closeness > cosine_score:
                    cosine_score, cosine_best = closeness, existing
            target = None
            similarity = 0.0
            if rule_best is not None and rule_score >= SIMILARITY_MERGE and rule_score >= cosine_score:
                target, similarity = rule_best, rule_score
            if cosine_best is not None and cosine_score >= COSINE_MERGE and cosine_score > similarity:
                target, similarity = cosine_best, cosine_score
            if target is not None:
                _merge_into(conn, target, tags, pinned, now)
                conn.commit()
                return {"id": target["id"], "updated": True, "merged": True, "similarity": round(similarity, 3)}
            cursor = conn.execute(
                "INSERT INTO lessons (text, tags, source, created_at, updated_at, enabled, pinned) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (text, tags, source, now, now, 1 if pinned else 0),
            )
            lesson_id = cursor.lastrowid
            if vector is not None:
                _store_vector(conn, lesson_id, vector)
            conn.commit()
            return {"id": lesson_id, "updated": False, "merged": False}
        finally:
            conn.close()


def update_lesson(
    lesson_id: int,
    text: str | None = None,
    tags: str | None = None,
    enabled: bool | None = None,
    pinned: bool | None = None,
) -> bool:
    if text is not None:
        if not text.strip():
            raise ValueError("Lesson text is required.")
        text = text.strip()
    if tags is not None:
        tags = tags.strip()
    if text is None and tags is None and enabled is None and pinned is None:
        return False
    values = (
        text,
        tags,
        None if enabled is None else (1 if enabled else 0),
        None if pinned is None else (1 if pinned else 0),
        time.time(),
        int(lesson_id),
    )
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            cursor = conn.execute(_UPDATE_SQL, values)
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def delete_lesson(lesson_id: int) -> bool:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            cursor = conn.execute("DELETE FROM lessons WHERE id = ?", (int(lesson_id),))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def clear_lessons() -> None:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            conn.execute("DELETE FROM lessons")
            conn.commit()
        finally:
            conn.close()


def _search_rows(conn: sqlite3.Connection, match: str, limit: int) -> list[tuple[int, dict[str, Any]]]:
    if limit and limit > 0:
        rows = conn.execute(_SEARCH_SQL + " LIMIT ?", (match, limit)).fetchall()
    else:
        rows = conn.execute(_SEARCH_SQL, (match,)).fetchall()
    return [(int(row[0]), _row_to_dict(row)) for row in rows]


def search(query: str, limit: int = 0, query_vec: Any = None) -> list[dict[str, Any]]:
    limit = _wanted(limit)
    match = fts_query(query, min_length=3, limit=0)
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            fts = _search_rows(conn, match, limit) if match else []
            vectors = _vector_results(conn, query_vec, limit) if query_vec is not None else []
            if not vectors:
                return [item for _lesson_id, item in fts]
            if not fts:
                return [item for _lesson_id, item in vectors]
            return _fuse([fts, vectors], limit)
        finally:
            conn.close()


def relevant(query: str, limit: int = 0, query_vec: Any = None) -> list[dict[str, Any]]:
    limit = _wanted(limit)
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            pinned = [_row_to_dict(row) for row in conn.execute(_SELECT_ENABLED_PINNED).fetchall()]
            seen = {item["id"] for item in pinned}
            fused: list[dict[str, Any]] = []
            if str(query or "").strip() or query_vec is not None:
                match = fts_query(query, min_length=3, limit=0)
                fts = _search_rows(conn, match, limit) if match else []
                vectors = _vector_results(conn, query_vec, limit) if query_vec is not None else []
                if fts and vectors:
                    combined = _fuse([fts, vectors], limit)
                elif fts:
                    combined = [item for _lesson_id, item in fts]
                else:
                    combined = [item for _lesson_id, item in vectors]
                for item in combined:
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        fused.append(item)
            else:
                if limit and limit > 0:
                    rows = conn.execute(_SELECT_ENABLED_LIMIT, (limit,)).fetchall()
                else:
                    rows = conn.execute(_SELECT_ENABLED).fetchall()
                for row in rows:
                    item = _row_to_dict(row)
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        fused.append(item)
            ordered = pinned + fused
            return ordered[:limit] if limit else ordered
        finally:
            conn.close()
