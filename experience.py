"""Approved workflow fragments and observed execution errors.

This is reference evidence, not policy: entries describe what happened under specific
conditions and must be re-checked against the current installation before reuse. It is stored
separately from the preference lessons in ``memory.py``.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Mapping

try:
    from .storage import connect, fts_query
except ImportError:
    from storage import connect, fts_query

try:
    from . import node_catalog
except ImportError:
    import node_catalog

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(NODE_DIR, "assistant_experience.sqlite")

_lock = threading.RLock()
_vectors_cache: dict[str, Any] = {"key": None, "count": -1, "ids": None, "matrix": None}

_OOM_RE = re.compile(r"out of memory|outofmemory|cuda.*memory|allocation|insufficient.*memory|\boom\b", re.I)
_MISSING_RE = re.compile(
    r"no such file|not found|filenotfound|cannot find|no file named|missing|"
    r"invalid.*(file|model|checkpoint|lora|vae)|\.safetensors|\.sft|\.ckpt|\.gguf|\.pth|\.bin",
    re.I,
)
_FAILURE_KINDS = {"missing_file", "oom", "invalid_connection", "runtime_error", "unsatisfactory", "unknown"}


def _connect() -> sqlite3.Connection:
    return connect(DB_PATH)


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS experiences (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            task TEXT,
            workflow_key TEXT,
            prompt_id TEXT,
            fragment TEXT,
            models TEXT,
            pack_fingerprints TEXT,
            node_types TEXT,
            execution_status TEXT,
            failure_kind TEXT,
            error TEXT,
            verdict TEXT DEFAULT 'none',
            verdict_note TEXT,
            source TEXT,
            created_at REAL,
            updated_at REAL,
            enabled INTEGER DEFAULT 1
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS experiences_fts USING fts5(
            task, node_types, error, content='experiences', content_rowid='id', tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS experiences_ai AFTER INSERT ON experiences BEGIN
            INSERT INTO experiences_fts(rowid, task, node_types, error)
            VALUES (new.id, new.task, new.node_types, new.error);
        END;
        CREATE TRIGGER IF NOT EXISTS experiences_ad AFTER DELETE ON experiences BEGIN
            INSERT INTO experiences_fts(experiences_fts, rowid, task, node_types, error)
            VALUES ('delete', old.id, old.task, old.node_types, old.error);
        END;
        CREATE TRIGGER IF NOT EXISTS experiences_au AFTER UPDATE ON experiences BEGIN
            INSERT INTO experiences_fts(experiences_fts, rowid, task, node_types, error)
            VALUES ('delete', old.id, old.task, old.node_types, old.error);
            INSERT INTO experiences_fts(rowid, task, node_types, error)
            VALUES (new.id, new.task, new.node_types, new.error);
        END;
        CREATE TABLE IF NOT EXISTS experience_vectors (
            experience_id INTEGER PRIMARY KEY,
            dim INTEGER,
            vec BLOB
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TRIGGER IF NOT EXISTS experiences_vectors_ad AFTER DELETE ON experiences BEGIN
            DELETE FROM experience_vectors WHERE experience_id = old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS experience_vectors_ai AFTER INSERT ON experience_vectors BEGIN
            INSERT INTO meta(key, value) VALUES ('experience_vectors_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS experience_vectors_au AFTER UPDATE ON experience_vectors BEGIN
            INSERT INTO meta(key, value) VALUES ('experience_vectors_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS experience_vectors_ade AFTER DELETE ON experience_vectors BEGIN
            INSERT INTO meta(key, value) VALUES ('experience_vectors_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        """
    )


def classify_failure(
    exception_type: str = "",
    exception_message: str = "",
    node_type: str = "",
    node_errors: Any = None,
) -> str:
    """Map an observed failure to a coarse kind (never a compatibility verdict)."""
    if node_errors:
        return "invalid_connection"
    text = f"{exception_type or ''} {exception_message or ''} {node_type or ''}".strip()
    if _OOM_RE.search(text):
        return "oom"
    if _MISSING_RE.search(text):
        return "missing_file"
    return "runtime_error" if text else "unknown"


def fingerprints_for(node_types: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for node_type in node_types:
        try:
            record = node_catalog.record(str(node_type))
        except Exception:
            continue
        if record.get("available") and not record.get("schema_error"):
            result[str(node_type)] = str(record.get("fingerprint") or "")
    return result


def revalidate(fragment: Any, pack_fingerprints: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Check a stored fragment's node types against the current installation."""
    stored = pack_fingerprints or {}
    nodes = fragment.get("nodes") if isinstance(fragment, Mapping) else None
    seen: dict[str, dict[str, Any]] = {}
    for node in nodes or []:
        if not isinstance(node, Mapping):
            continue
        node_type = str(node.get("type") or "")
        if not node_type or node_type in seen:
            continue
        try:
            record = node_catalog.record(node_type)
        except Exception:
            record = {"available": False}
        previous = str(stored.get(node_type) or "")
        seen[node_type] = {
            "type": node_type,
            "installed": bool(record.get("available") and not record.get("schema_error")),
            "schema_changed": bool(previous and record.get("available") and record.get("fingerprint") != previous),
        }
    return list(seen.values())


def _node_types(fragment: Any) -> list[str]:
    nodes = fragment.get("nodes") if isinstance(fragment, Mapping) else None
    names: list[str] = []
    for node in nodes or []:
        if isinstance(node, Mapping) and node.get("type"):
            name = str(node["type"])
            if name not in names:
                names.append(name)
    return names


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "kind": row[1],
        "task": row[2] or "",
        "workflow_key": row[3] or "",
        "prompt_id": row[4] or "",
        "fragment": json.loads(row[5]) if row[5] else {},
        "models": json.loads(row[6]) if row[6] else [],
        "pack_fingerprints": json.loads(row[7]) if row[7] else {},
        "node_types": (row[8] or "").split(),
        "execution_status": row[9] or "",
        "failure_kind": row[10] or "",
        "error": row[11] or "",
        "verdict": row[12] or "none",
        "verdict_note": row[13] or "",
        "source": row[14] or "",
        "created_at": row[15],
        "updated_at": row[16],
        "enabled": bool(row[17]),
    }


_COLUMNS = (
    "id, kind, task, workflow_key, prompt_id, fragment, models, pack_fingerprints, node_types, "
    "execution_status, failure_kind, error, verdict, verdict_note, source, created_at, updated_at, enabled"
)
_SELECT_ALL = "SELECT " + _COLUMNS + " FROM experiences"
_SELECT_ENABLED = _SELECT_ALL + " WHERE enabled = 1"
_SELECT_BY_ID = _SELECT_ALL + " WHERE id = ?"
_SELECT_ORDERED = _SELECT_ALL + " ORDER BY id DESC"
_FTS_SQL = (
    "SELECT " + ", ".join("e." + column for column in _COLUMNS.split(", "))
    + " FROM experiences_fts JOIN experiences e ON e.id = experiences_fts.rowid "
    "WHERE experiences_fts MATCH ? ORDER BY bm25(experiences_fts)"
)


def prune(keep: int) -> int:
    """Manually drop everything older than the newest ``keep`` records. Never automatic."""
    keep = int(keep or 0)
    if keep <= 0:
        return 0
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            cursor = conn.execute(
                "DELETE FROM experiences WHERE id NOT IN (SELECT id FROM experiences ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            conn.commit()
            return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        finally:
            conn.close()


def add_experience(record: Mapping[str, Any]) -> dict[str, Any]:
    fragment = record.get("fragment") if isinstance(record.get("fragment"), Mapping) else {}
    node_types = _node_types(fragment)
    kind = "success" if str(record.get("kind") or "") == "success" else "failure"
    execution_status = str(record.get("execution_status") or ("success" if kind == "success" else "error"))
    failure_kind = str(record.get("failure_kind") or "")
    if kind == "failure":
        if failure_kind not in _FAILURE_KINDS:
            failure_kind = classify_failure(
                exception_message=str(record.get("error") or ""),
                node_errors=record.get("node_errors"),
            )
    else:
        failure_kind = ""
    verdict = str(record.get("verdict") or ("pending" if kind == "success" else "none"))
    now = time.time()
    pack_fingerprints = fingerprints_for(node_types) if record.get("fingerprints") is None else record["fingerprints"]
    row = (
        kind,
        str(record.get("task") or ""),
        str(record.get("workflow_key") or ""),
        str(record.get("prompt_id") or ""),
        _json(fragment),
        _json(record.get("models") or []),
        _json(pack_fingerprints or {}),
        " ".join(node_types),
        execution_status,
        failure_kind,
        str(record.get("error") or ""),
        verdict,
        str(record.get("verdict_note") or ""),
        str(record.get("source") or "run"),
        now,
        now,
        1 if record.get("enabled", True) else 0,
    )
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            cursor = conn.execute(
                "INSERT INTO experiences (kind, task, workflow_key, prompt_id, fragment, models, "
                "pack_fingerprints, node_types, execution_status, failure_kind, error, verdict, "
                "verdict_note, source, created_at, updated_at, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.commit()
            return {"id": cursor.lastrowid, "kind": kind, "failure_kind": failure_kind, "verdict": verdict}
        finally:
            conn.close()


def update_verdict(experience_id: int, verdict: str, note: str = "", failure_kind: str = "") -> bool:
    verdict = str(verdict or "").strip().lower()
    if verdict not in {"approved", "rejected", "pending", "none"}:
        raise ValueError(f"Unknown verdict: {verdict}")
    kind = str(failure_kind or "")
    if kind and kind not in _FAILURE_KINDS:
        kind = ""
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            if kind:
                cursor = conn.execute(
                    "UPDATE experiences SET verdict = ?, verdict_note = ?, kind = 'failure', failure_kind = ?, "
                    "updated_at = ? WHERE id = ?",
                    (verdict, str(note or ""), kind, time.time(), int(experience_id)),
                )
            else:
                cursor = conn.execute(
                    "UPDATE experiences SET verdict = ?, verdict_note = ?, updated_at = ? WHERE id = ?",
                    (verdict, str(note or ""), time.time(), int(experience_id)),
                )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def list_experiences(enabled_only: bool = False) -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            rows = conn.execute(_SELECT_ENABLED if enabled_only else _SELECT_ORDERED).fetchall()
            return [_row_to_dict(row) for row in rows]
        finally:
            conn.close()


def delete_experience(experience_id: int) -> bool:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            cursor = conn.execute("DELETE FROM experiences WHERE id = ?", (int(experience_id),))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def set_enabled(experience_id: int, enabled: bool) -> bool:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            cursor = conn.execute(
                "UPDATE experiences SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, time.time(), int(experience_id)),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def clear_experiences() -> None:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            conn.execute("DELETE FROM experiences")
            conn.commit()
        finally:
            conn.close()


def _load_vectors(conn: sqlite3.Connection) -> tuple[Any, Any]:
    import numpy as np

    count = conn.execute("SELECT COUNT(*) FROM experience_vectors").fetchone()[0]
    revision = conn.execute("SELECT value FROM meta WHERE key = 'experience_vectors_revision'").fetchone()
    key = (os.path.realpath(DB_PATH), revision[0] if revision else "0")
    with _lock:
        if (_vectors_cache.get("key") == key and _vectors_cache.get("count") == count
                and _vectors_cache.get("matrix") is not None):
            return _vectors_cache["ids"], _vectors_cache["matrix"]
    ids: list[int] = []
    vectors = []
    for experience_id, _dim, blob in conn.execute("SELECT experience_id, dim, vec FROM experience_vectors"):
        ids.append(int(experience_id))
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


def _vector_rank(conn: sqlite3.Connection, query_vec: Any, limit: int) -> list[tuple[int, dict[str, Any]]]:
    if query_vec is None:
        return []
    import numpy as np

    ids, matrix = _load_vectors(conn)
    if matrix is None:
        return []
    query = np.asarray(query_vec, dtype="float32")
    if query.shape[0] != matrix.shape[1]:
        return []
    query = query / (np.linalg.norm(query) + 1e-9)
    scores = matrix @ query
    out: list[tuple[int, dict[str, Any]]] = []
    for position in np.argsort(-scores):
        experience_id = int(ids[position])
        row = conn.execute(_SELECT_BY_ID, (experience_id,)).fetchone()
        if not row:
            continue
        item = _row_to_dict(row)
        if not item["enabled"]:
            continue
        out.append((experience_id, item))
        if limit and len(out) >= limit:
            break
    return out


def _fts_rank(conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[int, dict[str, Any]]]:
    match = fts_query(query, min_length=3, limit=0)
    if not match:
        return []
    if limit and limit > 0:
        rows = conn.execute(_FTS_SQL + " LIMIT ?", (match, limit)).fetchall()
    else:
        rows = conn.execute(_FTS_SQL, (match,)).fetchall()
    return [(int(row[0]), _row_to_dict(row)) for row in rows]


def _fuse(ranked: list[list[tuple[int, dict[str, Any]]]], limit: int) -> list[dict[str, Any]]:
    scores: dict[int, float] = {}
    items: dict[int, dict[str, Any]] = {}
    for group in ranked:
        for rank, (experience_id, item) in enumerate(group):
            scores[experience_id] = scores.get(experience_id, 0.0) + 1.0 / (60 + rank)
            items.setdefault(experience_id, item)
    ordered = sorted(scores, key=lambda experience_id: -scores[experience_id])
    if limit and limit > 0:
        ordered = ordered[:limit]
    return [items[experience_id] for experience_id in ordered]


def _wanted(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def search(query: str, limit: int = 0, query_vec: Any = None) -> list[dict[str, Any]]:
    limit = _wanted(limit)
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            fts = _fts_rank(conn, query, limit)
            vectors = _vector_rank(conn, query_vec, limit)
            if fts and vectors:
                return _fuse([fts, vectors], limit)
            return [item for _id, item in (fts or vectors)]
        finally:
            conn.close()


def relevant(task: str, limit: int = 0, query_vec: Any = None) -> list[dict[str, Any]]:
    """Approved successes first, then matching failures, ranked by relevance."""
    limit = _wanted(limit)
    pool = limit * 4 if limit else 0
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            fts = _fts_rank(conn, task, pool)
            vectors = _vector_rank(conn, query_vec, pool)
            combined = _fuse([fts, vectors], pool) if (fts and vectors) else [
                item for _id, item in (fts or vectors)
            ]
            approved = [item for item in combined if item["kind"] == "success" and item["verdict"] == "approved"]
            failures = [item for item in combined if item["kind"] == "failure"]
            taken = {item["id"] for item in approved} | {item["id"] for item in failures}
            fallback = [item for item in combined if item["id"] not in taken]
            ordered = approved + failures + fallback
            return ordered[:limit] if limit else ordered
        finally:
            conn.close()


def set_vector(experience_id: int, vector: Any) -> bool:
    if vector is None:
        return False
    import numpy as np

    array = np.asarray(vector, dtype="float32")
    if array.size == 0:
        return False
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            conn.execute(
                "INSERT OR REPLACE INTO experience_vectors (experience_id, dim, vec) VALUES (?, ?, ?)",
                (int(experience_id), int(array.shape[0]), array.tobytes()),
            )
            conn.commit()
            _vectors_cache["count"] = -1
            return True
        finally:
            conn.close()


def pending_experiences() -> list[tuple[int, str]]:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            rows = conn.execute(
                "SELECT e.id, e.task, e.node_types, e.error FROM experiences e "
                "LEFT JOIN experience_vectors v ON v.experience_id = e.id "
                "WHERE e.enabled = 1 AND v.experience_id IS NULL ORDER BY e.id DESC"
            ).fetchall()
            return [(int(row[0]), " ".join(str(part) for part in (row[1], row[2], row[3]) if part)) for row in rows]
        finally:
            conn.close()


def vector_stats() -> dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            total = conn.execute("SELECT COUNT(*) FROM experiences WHERE enabled = 1").fetchone()[0]
            embedded = conn.execute(
                "SELECT COUNT(*) FROM experience_vectors v JOIN experiences e ON e.id = v.experience_id "
                "WHERE e.enabled = 1"
            ).fetchone()[0]
            model = conn.execute("SELECT value FROM meta WHERE key = 'experience_embed_model'").fetchone()
            error = conn.execute("SELECT value FROM meta WHERE key = 'experience_embed_error'").fetchone()
            return {
                "experiences": int(total),
                "embedded": int(embedded),
                "model": model[0] if model else "",
                "error": error[0] if error else "",
            }
        finally:
            conn.close()


def stored_embed_model() -> str:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute("SELECT value FROM meta WHERE key = 'experience_embed_model'").fetchone()
            return row[0] if row else ""
        finally:
            conn.close()


def ensure_embed_model(model: str) -> None:
    model = str(model or "")
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute("SELECT value FROM meta WHERE key = 'experience_embed_model'").fetchone()
            if (row[0] if row else "") != model:
                conn.execute("DELETE FROM experience_vectors")
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('experience_embed_model', ?)", (model,)
                )
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
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('experience_embed_error', ?)",
                (str(message or ""),),
            )
            conn.commit()
        finally:
            conn.close()
