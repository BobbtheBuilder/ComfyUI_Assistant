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


def fts_query(query: str, min_length: int = 2, limit: int = 0) -> str:
    """Build an OR query from tokens. ``limit`` <= 0 includes every token."""
    tokens = [token for token in re.findall(r"[A-Za-z0-9_]+", query or "") if len(token) >= min_length]
    if not tokens:
        return ""
    if limit and limit > 0:
        tokens = tokens[:limit]
    return " OR ".join(f'"{token}"' for token in tokens)
