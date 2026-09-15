from __future__ import annotations

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
        """
    )


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


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
_SEARCH_SQL = (
    "SELECT " + ", ".join("l." + column for column in _COLUMNS.split(", "))
    + " FROM lessons_fts JOIN lessons l ON l.id = lessons_fts.rowid "
    "WHERE lessons_fts MATCH ? AND l.enabled = 1 ORDER BY bm25(lessons_fts) LIMIT ?"
)
_UPDATE_SQL = (
    "UPDATE lessons SET text = COALESCE(?, text), tags = COALESCE(?, tags), "
    "enabled = COALESCE(?, enabled), pinned = COALESCE(?, pinned), updated_at = ? WHERE id = ?"
)


def list_lessons(enabled_only: bool = False) -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            query = _SELECT_ENABLED_ORDERED if enabled_only else _SELECT_ALL_ORDERED
            return [_row_to_dict(row) for row in conn.execute(query).fetchall()]
        finally:
            conn.close()


def add_lesson(text: str, tags: str = "", source: str = "user", pinned: bool = False) -> dict[str, Any]:
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
                    conn.execute(
                        "UPDATE lessons SET tags = ?, pinned = ?, updated_at = ? WHERE id = ?",
                        (tags or existing["tags"], 1 if pinned else existing["pinned"], now, existing["id"]),
                    )
                    conn.commit()
                    return {"id": existing["id"], "updated": True}
            cursor = conn.execute(
                "INSERT INTO lessons (text, tags, source, created_at, updated_at, enabled, pinned) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (text, tags, source, now, now, 1 if pinned else 0),
            )
            conn.commit()
            return {"id": cursor.lastrowid, "updated": False}
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


def search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    match = fts_query(query, min_length=3, limit=16)
    if not match:
        return []
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            rows = conn.execute(
                _SEARCH_SQL,
                (match, max(1, min(int(limit), 30))),
            ).fetchall()
            return [_row_to_dict(row) for row in rows]
        finally:
            conn.close()


def relevant(query: str, limit: int = 8) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 30))
    with _lock:
        conn = _connect()
        try:
            _init(conn)
            pinned = [_row_to_dict(row) for row in conn.execute(_SELECT_ENABLED_PINNED).fetchall()]
            results = list(pinned)
            seen = {item["id"] for item in results}
            if query.strip():
                for item in search(query, limit):
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        results.append(item)
            else:
                for row in conn.execute(_SELECT_ENABLED_LIMIT, (limit,)):
                    item = _row_to_dict(row)
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        results.append(item)
            return results[: max(limit, len(pinned))]
        finally:
            conn.close()
