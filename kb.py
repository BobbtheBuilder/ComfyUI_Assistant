from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any

try:
    from .debug import debug_log as _debug_log
except ImportError:
    from debug import debug_log as _debug_log

try:
    from .storage import connect, fts_query
except ImportError:
    from storage import connect, fts_query

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(NODE_DIR, "kb_index.sqlite")
CACHE_DIR = os.path.join(NODE_DIR, "kb_cache")
OFFICIAL_PATH = os.path.join(CACHE_DIR, "llms-full.txt")
OFFICIAL_URL = "https://docs.comfy.org/llms-full.txt"

EXCLUDED_DIRS = {
    ".git", "node_modules", "vendor", "llama_cpp_src", "site-packages",
    "__pycache__", ".venv", "venv", "env", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
}
DOC_EXTENSIONS = (".md", ".mdx", ".txt")
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
MAX_FILE_BYTES = 2_000_000

_lock = threading.RLock()
_state: dict[str, Any] = {
    "building": False,
    "phase": "",
    "progress_done": 0,
    "progress_total": 0,
    "last_build": 0.0,
    "last_build_iso": "",
    "chunks": 0,
    "files": 0,
    "official_fetched_at": 0.0,
    "official_fetched_iso": "",
    "official_chunks": 0,
    "error": "",
}


def _set_progress(phase: str, done: int = 0, total: int = 0) -> None:
    with _lock:
        _state["phase"] = phase
        _state["progress_done"] = done
        _state["progress_total"] = total


def _iso(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _custom_node_dirs() -> list[str]:
    try:
        import folder_paths
    except ImportError:
        return []
    paths = folder_paths.folder_names_and_paths.get("custom_nodes", ([], set()))[0]
    return [os.path.realpath(path) for path in paths if os.path.isdir(path)]


def _connect() -> sqlite3.Connection:
    return connect(DB_PATH)


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source TEXT NOT NULL,
            pack TEXT,
            path TEXT,
            url TEXT,
            title TEXT,
            heading TEXT,
            content TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content, title, heading, pack, source,
            content='chunks', content_rowid='id', tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, content, title, heading, pack, source)
            VALUES (new.id, new.content, new.title, new.heading, new.pack, new.source);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content, title, heading, pack, source)
            VALUES ('delete', old.id, old.content, old.title, old.heading, old.pack, old.source);
        END;
        """
    )


def _meta_get(key: str) -> str:
    conn = _connect()
    try:
        _init(conn)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def _meta_set(key: str, value: str) -> None:
    conn = _connect()
    try:
        _init(conn)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def _split_long(text: str, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind("\n\n", start, end)
            if cut == -1 or cut <= start + max_chars // 2:
                cut = text.rfind("\n", start, end)
            end = cut if cut > start + max_chars // 2 else end
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return pieces


def _chunks_for_text(text: str) -> list[tuple[str, str]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    result: list[tuple[str, str]] = []
    for section in re.split(r"\n(?=#{1,6}\s)", text):
        section = section.strip()
        if not section:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)", section)
        heading = match.group(2).strip() if match else ""
        for piece in _split_long(section):
            result.append((heading, piece))
    return result


def _pack_for_path(path: str, root: str) -> str:
    if not path.startswith(root + os.sep):
        return ""
    return os.path.relpath(path, root).split(os.sep)[0]


def _pack_from_module(module: str) -> str:
    if not module or not module.startswith("custom_nodes."):
        return ""
    return module.split(".", 2)[1]


def _insert_chunks(conn: sqlite3.Connection, records: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO chunks (source_kind, source, pack, path, url, title, heading, content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        records,
    )


def index_local() -> int:
    roots = _custom_node_dirs()
    if not roots:
        return 0
    conn = _connect()
    try:
        _init(conn)
        _set_progress("Scanning pack docs")
        current: dict[str, tuple[float, int, str, str]] = {}
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS]
                for name in filenames:
                    if not name.lower().endswith(DOC_EXTENSIONS):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        stat = os.stat(full)
                    except OSError:
                        continue
                    if stat.st_size > MAX_FILE_BYTES:
                        continue
                    pack = _pack_for_path(full, root)
                    rel = os.path.relpath(full, root)
                    current[full] = (stat.st_mtime, stat.st_size, pack, rel)

        known = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT path, mtime, size FROM files")}
        changed = [path for path, meta in current.items() if known.get(path) != (meta[0], meta[1])]
        removed = [path for path in known if path not in current]

        for path in removed:
            conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
            conn.execute("DELETE FROM files WHERE path = ?", (path,))
        total = len(changed)
        _set_progress("Indexing pack docs", 0, total)
        for index, path in enumerate(changed, start=1):
            conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            mtime, size, pack, rel = current[path]
            title = f"{pack}/{rel}" if pack else rel
            records = [
                ("pack", pack, pack, path, "", title, heading, content)
                for heading, content in _chunks_for_text(text)
            ]
            _insert_chunks(conn, records)
            conn.execute("INSERT OR REPLACE INTO files (path, mtime, size) VALUES (?, ?, ?)", (path, mtime, size))
            if index % 25 == 0 or index == total:
                _set_progress("Indexing pack docs", index, total)
        conn.commit()
        return len(changed)
    finally:
        conn.close()


def index_node_defs() -> int:
    try:
        import nodes as comfy_nodes
    except Exception:
        return 0
    mappings = getattr(comfy_nodes, "NODE_CLASS_MAPPINGS", {})
    display_names = getattr(comfy_nodes, "NODE_DISPLAY_NAME_MAPPINGS", {})
    conn = _connect()
    try:
        _init(conn)
        conn.execute("DELETE FROM chunks WHERE source_kind = 'node'")
        records = []
        total = len(mappings)
        _set_progress("Indexing node descriptions", 0, total)
        for index, (node_type, node_cls) in enumerate(mappings.items(), start=1):
            description = str(getattr(node_cls, "DESCRIPTION", "") or "").strip()
            module = str(getattr(node_cls, "RELATIVE_PYTHON_MODULE", "") or "")
            pack = _pack_from_module(module)
            if description or pack:
                category = str(getattr(node_cls, "CATEGORY", "") or "").strip()
                display = display_names.get(node_type, node_type)
                content = f"{display} ({node_type})"
                if category:
                    content += f"\nCategory: {category}"
                if pack:
                    content += f"\nPack: {pack}"
                if description:
                    content += f"\n{description}"
                records.append(("node", node_type, pack, None, "", display, category, content))
            if index % 200 == 0 or index == total:
                _set_progress("Indexing node descriptions", index, total)
        _insert_chunks(conn, records)
        conn.commit()
        return len(records)
    finally:
        conn.close()


_MDX_TAG_RE = re.compile(r"</?(?:Card|CardGroup|Info|Note|Warning|Tip|Tabs|Tab|Accordion|AccordionGroup|Steps|Step|Frame|Columns|Column|Icon|Tooltip|Badge)\b[^>]*>")
_HTML_TAG_RE = re.compile(r"</?(?:div|img|a|br|span|p|ul|ol|li|table|tr|td|th|pre|code)\b[^>]*>")
_MDX_COMMENT_RE = re.compile(r"\{/\*.*?\*/\}", re.S)


def _clean_official(text: str) -> str:
    text = _MDX_COMMENT_RE.sub("", text)
    text = _MDX_TAG_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _index_official_text(text: str) -> int:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    pages = [page.strip() for page in re.split(r"(?m)^#\s+", text) if page.strip()]
    total = len(pages)
    _set_progress("Indexing official docs", 0, total)
    conn = _connect()
    try:
        _init(conn)
        conn.execute("DELETE FROM chunks WHERE source_kind = 'official'")
        records = []
        for index, page in enumerate(pages, start=1):
            lines = page.split("\n")
            title = lines[0].strip()
            url = ""
            body_start = 1
            for line_index in range(1, min(len(lines), 6)):
                if lines[line_index].startswith("Source:"):
                    url = lines[line_index][7:].strip()
                    body_start = line_index + 1
                    break
            body = _clean_official("\n".join(lines[body_start:]).strip())
            for heading, content in _chunks_for_text(body):
                records.append(("official", url or title, "", None, url, title, heading, content))
            if index % 50 == 0 or index == total:
                _set_progress("Indexing official docs", index, total)
        _insert_chunks(conn, records)
        conn.commit()
        return len(records)
    finally:
        conn.close()


def fetch_official(force: bool = False) -> bool:
    if not force and os.path.isfile(OFFICIAL_PATH) and _meta_get("official_fetched_at"):
        return False
    os.makedirs(CACHE_DIR, exist_ok=True)
    request = urllib.request.Request(OFFICIAL_URL, headers={"User-Agent": "ComfyUI-Assistant-KB"})
    _set_progress("Downloading official docs", 0, 0)
    with urllib.request.urlopen(request, timeout=120) as response:
        total_bytes = int(response.headers.get("Content-Length") or 0)
        chunks = []
        received = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            _set_progress("Downloading official docs", received, total_bytes)
        data = b"".join(chunks)
    if not data:
        raise RuntimeError("official docs download was empty")
    with open(OFFICIAL_PATH, "wb") as handle:
        handle.write(data)
    _index_official_text(data.decode("utf-8", errors="replace"))
    _meta_set("official_fetched_at", str(time.time()))
    return True


def _refresh_counts() -> None:
    conn = _connect()
    try:
        _init(conn)
        _state["chunks"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        _state["files"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        _state["official_chunks"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_kind = 'official'"
        ).fetchone()[0]
        fetched = _meta_get("official_fetched_at")
        _state["official_fetched_at"] = float(fetched) if fetched else 0.0
        _state["official_fetched_iso"] = _iso(_state["official_fetched_at"])
    finally:
        conn.close()


def build(force_official: bool = False, auto_official: bool = True, refresh_days: int = 7, delay: float = 0) -> None:
    if delay:
        time.sleep(delay)
    with _lock:
        if _state["building"]:
            return
        _state["building"] = True
        _state["error"] = ""
    started = time.time()
    _debug_log("kb", "build.start", force_official=force_official, auto_official=auto_official)
    try:
        conn = _connect()
        try:
            _init(conn)
        finally:
            conn.close()
        index_local()
        index_node_defs()
        if auto_official:
            fetched_at = float(_meta_get("official_fetched_at") or 0)
            stale = (time.time() - fetched_at) > max(1, refresh_days) * 86400
            if force_official or not fetched_at or stale:
                try:
                    fetch_official(force=True)
                except Exception as exc:
                    _state["error"] = f"official docs: {exc}"
        _refresh_counts()
        _state["last_build"] = time.time()
        _state["last_build_iso"] = _iso(_state["last_build"])
        _set_progress("Ready")
        _debug_log(
            "kb",
            "build.done",
            ms=round((time.time() - started) * 1000),
            chunks=_state.get("chunks"),
            files=_state.get("files"),
            official_chunks=_state.get("official_chunks"),
            error=_state.get("error") or None,
        )
    except Exception as exc:
        _set_progress("Failed")
        _state["error"] = str(exc)
        _debug_log("kb", "build.error", level="error", error=str(exc))
    finally:
        with _lock:
            _state["building"] = False


def start_background(
    auto_official: bool = True,
    refresh_days: int = 7,
    force_official: bool = False,
    delay: float = 0,
) -> None:
    thread = threading.Thread(
        target=build,
        kwargs={
            "force_official": force_official,
            "auto_official": auto_official,
            "refresh_days": refresh_days,
            "delay": delay,
        },
        name="ComfyUIAssistantKB",
        daemon=True,
    )
    thread.start()


def status() -> dict[str, Any]:
    with _lock:
        building = _state["building"]
    if not building:
        try:
            _refresh_counts()
        except Exception:
            pass
    with _lock:
        return dict(_state)


def _snippet(content: str, query: str, length: int = 500) -> str:
    if len(content) <= length:
        return content
    lowered = content.lower()
    position = -1
    for token in re.findall(r"[A-Za-z0-9_]+", query.lower()):
        position = lowered.find(token)
        if position != -1:
            break
    if position == -1:
        return content[:length].strip() + "\u2026"
    start = max(0, position - length // 3)
    return ("\u2026" if start else "") + content[start:start + length].strip() + "\u2026"


def _row_to_result(row: Any, query: str) -> dict[str, Any]:
    return {
        "source_kind": row[0],
        "source": row[1],
        "pack": row[2] or "",
        "path": row[3] or "",
        "url": row[4] or "",
        "title": row[5] or "",
        "heading": row[6] or "",
        "snippet": _snippet(row[7], query),
    }


def _search_conn(conn: sqlite3.Connection, query: str, limit: int, source: str | None) -> list[dict[str, Any]]:
    match = fts_query(query, min_length=2, limit=12)
    if not match:
        return []
    sql = (
        "SELECT c.source_kind, c.source, c.pack, c.path, c.url, c.title, c.heading, c.content "
        "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ?"
    )
    params: list[Any] = [match]
    if source:
        sql += " AND c.source_kind = ?"
        params.append(source)
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    params.append(max(1, min(limit, 20)))
    return [_row_to_result(row, query) for row in conn.execute(sql, params).fetchall()]


def search(query: str, limit: int = 6, source: str | None = None) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        _init(conn)
        return _search_conn(conn, query, limit, source)
    finally:
        conn.close()


def node_docs(node_type: str, limit: int = 8) -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        row = conn.execute(
            "SELECT pack FROM chunks WHERE source_kind = 'node' AND source = ? LIMIT 1",
            (node_type,),
        ).fetchone()
        pack = row[0] if row else ""
        results = [
            _row_to_result(item, node_type)
            for item in conn.execute(
                "SELECT source_kind, source, pack, path, url, title, heading, content "
                "FROM chunks WHERE source_kind = 'node' AND source = ? LIMIT 2",
                (node_type,),
            ).fetchall()
        ]
        if pack:
            results.extend(
                _row_to_result(item, node_type)
                for item in conn.execute(
                    "SELECT source_kind, source, pack, path, url, title, heading, content "
                    "FROM chunks WHERE source_kind = 'pack' AND pack = ? LIMIT ?",
                    (pack, max(1, limit - len(results))),
                ).fetchall()
            )
        if len(results) < limit:
            seen = {(item["source_kind"], item["path"], item["title"], item["heading"]) for item in results}
            for item in _search_conn(conn, node_type, limit, source=None):
                key = (item["source_kind"], item["path"], item["title"], item["heading"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
                if len(results) >= limit:
                    break
        if not pack:
            pack = next((item["pack"] for item in results if item["pack"]), "")
        return {"node_type": node_type, "pack": pack, "results": results}
    finally:
        conn.close()
