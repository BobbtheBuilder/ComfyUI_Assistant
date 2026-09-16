from __future__ import annotations

import os
import re
import sqlite3


def connect(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    if not row:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    try:
        return int(row[0]) if row and row[0] else 0
    except (TypeError, ValueError):
        return 0


def fts_query(query: str, min_length: int = 2, limit: int = 16) -> str:
    tokens = [token for token in re.findall(r"[A-Za-z0-9_]+", query or "") if len(token) >= min_length]
    if not tokens:
        return ""
    return " OR ".join(f'"{token}"' for token in tokens[:limit])
