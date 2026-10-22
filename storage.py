from __future__ import annotations

import os
import re
import sqlite3
from typing import Any


def configured_limit(name: str, default: int = 0) -> int:
    """Read shared configuration lazily, including during startup diagnostics."""
    try:
        try:
            from .config_store import CONFIG_STORE
        except ImportError:
            from config_store import CONFIG_STORE
        return CONFIG_STORE.get_limit(name, default)
    except Exception:
        return default


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


def fuse_ranked(ranked_lists: list[list[tuple[int, dict[str, Any]]]], limit: int) -> list[dict[str, Any]]:
    scores: dict[int, float] = {}
    items: dict[int, dict[str, Any]] = {}
    for ranked in ranked_lists:
        for rank, (item_id, item) in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (60 + rank)
            items.setdefault(item_id, item)
    ordered = sorted(scores, key=lambda item_id: -scores[item_id])
    if limit and limit > 0:
        ordered = ordered[:limit]
    return [items[item_id] for item_id in ordered]


def wanted(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0
