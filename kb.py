from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime
from typing import Any, Mapping

try:
    from . import node_catalog
    from .embedding_text import embedding_parts
except ImportError:
    import node_catalog
    from embedding_text import embedding_parts

try:
    from .debug import debug_log as _debug_log
except ImportError:
    from debug import debug_log as _debug_log

try:
    from .storage import connect, fts_query, wanted as _wanted, configured_limit as _limit
except ImportError:
    from storage import connect, fts_query, wanted as _wanted, configured_limit as _limit

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


# Bump when the indexing logic changes so the next start rebuilds once instead of
# skipping on a stale fingerprint.
KB_BUILD_VERSION = 1

EXAMPLE_DIR_NAMES = {"example_workflows", "example_workflow", "examples", "workflows"}
README_URL = "https://raw.githubusercontent.com/comfyanonymous/ComfyUI/master/README.md"
EMBED_BATCH = 32


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
    "pack_chunks": 0,
    "node_chunks": 0,
    "example_chunks": 0,
    "registry_chunks": 0,
    "model_chunks": 0,
    "official_chunks": 0,
    "embedded": 0,
    "embed_model": "",
    "embed_window_tokens": 0,
    "embed_window_source": "",
    "embed_window_observed_tokens": 0,
    "embed_window_observed_source": "",
    "embed_reason": "",
    "embed_endpoint_confirmation_required": False,
    "embed_current_identity": "",
    "embed_previous_identity": "",
    "embed_error": "",
    "official_fetched_at": 0.0,
    "official_fetched_iso": "",
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


def _is_generated_graft_dir(path: str) -> bool:
    """Recognize generated graft output by its marker, not its name alone."""
    if os.path.basename(path).lower() != "graft":
        return False
    try:
        with open(os.path.join(path, "INDEX.md"), "r", encoding="utf-8", errors="replace") as handle:
            return handle.readline().strip().lower().startswith("# graft ")
    except OSError:
        return False


def _generated_graft_path(path: str) -> bool:
    parts = os.path.realpath(path).split(os.sep)
    for index, part in enumerate(parts):
        if part.lower() == "graft":
            candidate = os.sep.join(parts[:index + 1]) or os.sep
            if _is_generated_graft_dir(candidate):
                return True
    return False


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
        CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_kind, source);
        CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS node_records (name TEXT PRIMARY KEY, record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS node_examples (node_type TEXT, path TEXT, record TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS node_examples_type ON node_examples(node_type);
        CREATE TABLE IF NOT EXISTS embeddings (chunk_id INTEGER PRIMARY KEY, dim INTEGER, vec BLOB);
        CREATE TABLE IF NOT EXISTS embedding_fragments (
            chunk_id INTEGER, part INTEGER, dim INTEGER, vec BLOB,
            PRIMARY KEY (chunk_id, part)
        );
        CREATE TRIGGER IF NOT EXISTS embeddings_fragments_ad AFTER DELETE ON embeddings BEGIN
            DELETE FROM embedding_fragments WHERE chunk_id = old.chunk_id;
        END;
        CREATE TRIGGER IF NOT EXISTS fragments_ai AFTER INSERT ON embedding_fragments BEGIN
            INSERT INTO meta(key, value) VALUES ('embeddings_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS fragments_au AFTER UPDATE ON embedding_fragments BEGIN
            INSERT INTO meta(key, value) VALUES ('embeddings_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS fragments_ad AFTER DELETE ON embedding_fragments BEGIN
            INSERT INTO meta(key, value) VALUES ('embeddings_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
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
        CREATE TRIGGER IF NOT EXISTS chunks_embeddings_ad AFTER DELETE ON chunks BEGIN
            DELETE FROM embeddings WHERE chunk_id = old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS embeddings_ai AFTER INSERT ON embeddings BEGIN
            INSERT INTO meta(key, value) VALUES ('embeddings_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS embeddings_au AFTER UPDATE ON embeddings BEGIN
            INSERT INTO meta(key, value) VALUES ('embeddings_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS embeddings_ad AFTER DELETE ON embeddings BEGIN
            INSERT INTO meta(key, value) VALUES ('embeddings_revision', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
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


def _chunks_for_text(text: str, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[tuple[str, str]]:
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
        for piece in _split_long(section, max_chars, overlap):
            result.append((heading, piece))
    return result


def _effective_chunk_chars(refresh: bool = False) -> tuple[int, int]:
    """Chunk size/overlap for indexing: an explicit setting, else the embedding budget."""
    overlap = _limit("kb_chunk_overlap", 150) or 0
    configured = _limit("kb_chunk_chars", 0)
    if configured > 0:
        return configured, overlap
    if not refresh:
        try:
            previous = json.loads(_meta_get("node_chunking") or "null")
            if previous and previous[0] > 0:
                return int(previous[0]), overlap
        except (ValueError, TypeError, sqlite3.Error):
            pass
    try:
        config, model = _embed_settings()
        if config is not None:
            resolved = _resolve_embed_model(config, model)
            if resolved:
                _tokens, chars, source = asyncio.run(_providers_module().embedding_limits(config, resolved))
                if chars > 0 and source != "fallback":
                    return chars, overlap
    except Exception:
        pass
    per_token = _limit("embed_chars_per_token", 4) or 4
    fallback = _limit("embed_fallback_tokens", 512) or 512
    return max(1, int(fallback * per_token * 0.9)), overlap


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
                dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS
                               and not _is_generated_graft_dir(os.path.join(dirpath, name))]
                for name in filenames:
                    if not name.lower().endswith(DOC_EXTENSIONS):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        stat = os.stat(full)
                    except OSError:
                        continue
                    max_bytes = _limit("kb_max_file_bytes", 0)
                    if max_bytes and stat.st_size > max_bytes:
                        continue
                    pack = _pack_for_path(full, root)
                    rel = os.path.relpath(full, root)
                    current[full] = (stat.st_mtime, stat.st_size, pack, rel)

        known = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT path, mtime, size FROM files")}
        # Remove derived records for generated graph documents indexed before exclusion.
        # The source files remain untouched.
        for path in [path for path in known if _generated_graft_path(path)]:
            conn.execute("DELETE FROM chunks WHERE source_kind = 'pack' AND path = ?", (path,))
            conn.execute("DELETE FROM files WHERE path = ?", (path,))
            known.pop(path, None)
        changed = [path for path, meta in current.items() if known.get(path) != (meta[0], meta[1])]
        removed = [path for path in known if path not in current]

        for path in removed:
            conn.execute("DELETE FROM chunks WHERE source_kind = 'pack' AND path = ?", (path,))
            conn.execute("DELETE FROM files WHERE path = ?", (path,))
        total = len(changed)
        _set_progress("Indexing pack docs", 0, total)
        for index, path in enumerate(changed, start=1):
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
            _sync_chunks(conn, records, "source_kind = 'pack' AND path = ?", (path,))
            conn.execute("INSERT OR REPLACE INTO files (path, mtime, size) VALUES (?, ?, ?)", (path, mtime, size))
            if index % 25 == 0 or index == total:
                _set_progress("Indexing pack docs", index, total)
        conn.commit()
        return len(changed)
    finally:
        conn.close()


def _spec_text(spec: Any) -> str:
    if not isinstance(spec, (list, tuple)):
        return str(spec)
    try:
        type_part = spec[0]
        options = spec[1] if len(spec) > 1 else {}
    except (TypeError, IndexError):
        return str(spec)
    if isinstance(type_part, (list, tuple)):
        type_text = "enum(" + ", ".join(str(value) for value in type_part) + ")"
    else:
        type_text = str(type_part)
    if not isinstance(options, dict):
        return type_text
    extras = [f"{key}={options[key]}" for key in ("default", "min", "max", "step") if key in options]
    if options.get("tooltip"):
        extras.append(f"tooltip={options['tooltip']}")
    return type_text + ((" " + ", ".join(extras)) if extras else "")


def _node_content(info: dict[str, Any]) -> str:
    name = str(info.get("name") or "")
    lines = [f"{info.get('display_name') or name} ({name})"]
    if info.get("category"):
        lines.append(f"Category: {info['category']}")
    if info.get("python_module"):
        lines.append(f"Module: {info['python_module']}")
    if info.get("description"):
        lines.append(str(info["description"]))
    inputs = info.get("input") or {}
    for group in ("required", "optional"):
        for input_name, spec in (inputs.get(group) or {}).items():
            lines.append(f"input[{group}] {input_name}: {_spec_text(spec)}")
    outputs = list(info.get("output") or [])
    output_names = list(info.get("output_name") or outputs)
    for index, output_type in enumerate(outputs):
        output_name = output_names[index] if index < len(output_names) else output_type
        lines.append(f"output {output_name}: {output_type}")
    if info.get("output_node"):
        lines.append("output node")
    return "\n".join(lines)


def _collect_node_records() -> dict[str, dict[str, Any]]:
    inventory = node_catalog.snapshot()
    records = {}
    _set_progress("Reading local node inventory", 0, len(inventory[0]))
    for index, name in enumerate(inventory[0], 1):
        try:
            records[name] = node_catalog.record(name, inventory=inventory)
        except Exception as exc:
            records[name] = {"name": name, "available": True, "schema_error": str(exc)}
        if index % 200 == 0 or index == len(inventory[0]):
            _set_progress("Reading local node inventory", index, len(inventory[0]))
    return records


def _sync_chunks(conn, records, where: str, parameters=()) -> None:
    """Reconcile a source scope without replacing unchanged IDs or vectors."""
    existing = {}
    for row in conn.execute(
        "SELECT id, source_kind, source, pack, path, url, title, heading, content FROM chunks WHERE " + where,
        parameters,
    ):
        existing.setdefault(tuple(row[1:]), []).append(row[0])
    additions = []
    for record in records:
        ids = existing.get(tuple(record))
        if ids:
            ids.pop()
        else:
            additions.append(record)
    conn.executemany("DELETE FROM chunks WHERE id = ?",
                     ((chunk_id,) for ids in existing.values() for chunk_id in ids))
    _insert_chunks(conn, additions)


def index_node_schemas(node_records=None, chunking=None) -> int:
    node_records = _collect_node_records() if node_records is None else node_records
    conn = _connect()
    try:
        _init(conn)
        records = []
        chunk_chars, chunk_overlap = chunking or _effective_chunk_chars()
        total = len(node_records)
        _set_progress("Indexing node schemas", 0, total)
        for index, (node_type, info) in enumerate(node_records.items(), start=1):
            serialized = json.dumps(info, sort_keys=True)
            conn.execute("INSERT INTO node_records VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET record=excluded.record "
                         "WHERE record != excluded.record", (node_type, serialized))
            if info.get("schema_error"):
                continue
            content = _node_content(info)
            purpose = info.get("purpose", {}).get("text", "")
            if purpose and purpose != info.get("description"):
                content += "\nSource docstring: " + purpose
            pack = _pack_from_module(str(info.get("python_module") or ""))
            display = str(info.get("display_name") or node_type)
            category = str(info.get("category") or "")
            for _heading, piece in _chunks_for_text(content, chunk_chars, chunk_overlap):
                records.append(("node", node_type, pack, None, "", display, category, piece))
            if index % 200 == 0 or index == total:
                _set_progress("Indexing node schemas", index, total)
        # Keep unavailable records for coverage diagnostics; only reconcile this inventory.
        # Failed schemas must not leave old, apparently valid search chunks behind.
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('node_chunking', ?)",
                     (json.dumps([chunk_chars, chunk_overlap]),))
        conn.execute("CREATE TEMP TABLE current_nodes (name TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO current_nodes VALUES (?)", ((name,) for name in node_records))
        _sync_chunks(conn, records, "source_kind = 'node' AND source IN (SELECT name FROM current_nodes)")
        conn.commit()
        return len(records)
    finally:
        conn.close()


def _workflow_summary(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    types: list[str] = []
    settings: list[str] = []
    nodes = data.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type") or "")
            if not node_type:
                continue
            types.append(node_type)
            widgets = node.get("widgets_values")
            if isinstance(widgets, list) and widgets:
                values = ", ".join(str(value) for value in widgets)
                settings.append(f"{node_type}: {values}")
    elif data and all(isinstance(value, dict) and "class_type" in value for value in data.values()):
        for value in data.values():
            node_type = str(value.get("class_type") or "")
            types.append(node_type)
            inputs = value.get("inputs") or {}
            values = ", ".join(f"{key}={str(item)}" for key, item in inputs.items())
            if values:
                settings.append(f"{node_type}: {values}")
    else:
        return ""
    if not types:
        return ""
    counts = Counter(types)
    lines = ["Example workflow."]
    lines.append("Nodes: " + ", ".join(f"{name} x{count}" if count > 1 else name for name, count in sorted(counts.items())))
    if settings:
        lines.append("Settings: " + " | ".join(settings))
    return "\n".join(lines)


def index_examples(chunking=None) -> int:
    roots = _custom_node_dirs()
    conn = _connect()
    try:
        _init(conn)
        conn.execute("DELETE FROM node_examples")
        records = []
        chunk_chars, chunk_overlap = chunking or _effective_chunk_chars()
        _set_progress("Indexing example workflows", 0, 0)
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS
                               and not _is_generated_graft_dir(os.path.join(dirpath, name))]
                if not any(part.lower() in EXAMPLE_DIR_NAMES for part in os.path.relpath(dirpath, root).split(os.sep)):
                    continue
                for name in filenames:
                    if not name.lower().endswith(".json"):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        max_bytes = _limit("kb_max_file_bytes", 0)
                        if max_bytes and os.path.getsize(full) > max_bytes:
                            continue
                        with open(full, "r", encoding="utf-8", errors="replace") as handle:
                            data = json.load(handle)
                    except (OSError, ValueError):
                        continue
                    summary = _workflow_summary(data)
                    if not summary:
                        continue
                    for example in node_catalog.example_nodes(data):
                        conn.execute("INSERT INTO node_examples VALUES (?, ?, ?)",
                                     (example["node_type"], full, json.dumps(example)))
                    pack = _pack_for_path(full, root)
                    title = os.path.splitext(name)[0]
                    for _heading, piece in _chunks_for_text(summary, chunk_chars, chunk_overlap):
                        records.append(("example", full, pack, full, "", title, "", piece))
        _sync_chunks(conn, records, "source_kind = 'example'")
        conn.commit()
        return len(records)
    finally:
        conn.close()


def _manager_dir() -> str:
    for root in _custom_node_dirs():
        candidate = os.path.join(root, "ComfyUI-Manager")
        if os.path.isdir(candidate):
            return candidate
    return ""


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def index_registry(chunking=None) -> int:
    manager = _manager_dir()
    if not manager:
        return 0
    conn = _connect()
    try:
        _init(conn)
        records = []
        chunk_chars, chunk_overlap = chunking or _effective_chunk_chars()

        def add(kind, source, pack, path, url, title, content):
            for _heading, piece in _chunks_for_text(content, chunk_chars, chunk_overlap):
                records.append((kind, source, pack, path, url, title, "", piece))

        _set_progress("Indexing package registry", 0, 0)

        pack_list = _load_json(os.path.join(manager, "custom-node-list.json"))
        for pack in (pack_list or {}).get("custom_nodes", []):
            if not isinstance(pack, dict):
                continue
            title = str(pack.get("title") or "").strip()
            reference = str(pack.get("reference") or "").strip()
            if not title and not reference:
                continue
            content = f"Custom node pack: {title}\nRepository: {reference}\nInstall: {pack.get('install_type', '')}"
            description = str(pack.get("description") or "").strip()
            if description:
                content += f"\n{description}"
            author = str(pack.get("author") or "").strip()
            if author:
                content += f"\nAuthor: {author}"
            add("registry", title or reference, title, None, reference, title, content)

        node_map = _load_json(os.path.join(manager, "extension-node-map.json"))
        if isinstance(node_map, dict):
            for reference, entry in node_map.items():
                node_names: list[str] = []
                title_aux = ""
                if isinstance(entry, list) and entry:
                    if isinstance(entry[0], list):
                        node_names = [str(name) for name in entry[0]]
                    if len(entry) > 1 and isinstance(entry[1], dict):
                        title_aux = str(entry[1].get("title_aux") or "")
                if not node_names:
                    continue
                pack = title_aux or reference
                content = f"Nodes from pack {pack}:\n{', '.join(node_names)}\nRepository: {reference}"
                add("registry", pack, pack, None, reference, pack, content)

        model_list = _load_json(os.path.join(manager, "model-list.json"))
        for model in (model_list or {}).get("models", []):
            if not isinstance(model, dict):
                continue
            name = str(model.get("name") or "").strip()
            if not name:
                continue
            content = (
                f"Model: {name}\nType: {model.get('type', '')}\nBase: {model.get('base', '')}\n"
                f"Save to: {model.get('save_path', '')}"
            )
            description = str(model.get("description") or "").strip()
            if description:
                content += f"\n{description}"
            reference = str(model.get("reference") or model.get("url") or "").strip()
            if reference:
                content += f"\nReference: {reference}"
            add("model", name, "", None, reference, name, content)

        _sync_chunks(conn, records, "source_kind IN ('registry', 'model')")
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
        _sync_chunks(conn, records, "source_kind = 'official' AND source NOT LIKE 'ext:%'")
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


def _fetch_url_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-Assistant-KB"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def index_extended_official() -> int:
    conn = _connect()
    try:
        _init(conn)
        records = []
        _set_progress("Indexing extended docs", 0, 0)
        try:
            readme = _fetch_url_text(README_URL)
            url = "ext:comfyui-readme"
            for heading, content in _chunks_for_text(_clean_official(readme)):
                records.append(("official", url, "", None, url, "ComfyUI README", heading, content))
        except Exception as exc:
            _debug_log("kb", "readme.fetch_error", level="warning", error=str(exc))
            raise
        _sync_chunks(conn, records, "source_kind = 'official' AND source LIKE 'ext:%'")
        conn.commit()
        return len(records)
    finally:
        conn.close()


def _kb_config() -> dict[str, Any]:
    try:
        try:
            from .config_store import CONFIG_STORE
        except ImportError:
            from config_store import CONFIG_STORE
        return CONFIG_STORE.resolved().get("kb", {}) or {}
    except Exception:
        return {}


def _stat_stamp(path: str) -> list[Any]:
    try:
        stat = os.stat(path)
    except OSError:
        return [None, None]
    return [stat.st_size, stat.st_mtime_ns]


def _build_fingerprint(node_records=None, chunking=None, components=None, kb_config=None) -> str:
    """Cheap signature of everything the indexers depend on.

    Build supplies the exact node records and chunking it will index. File stamps
    detect local document/example changes; source and schema evidence is in the records.
    """
    parts: dict[str, Any] = {"version": KB_BUILD_VERSION}
    if node_records is None:
        try:
            mapping, _ = node_catalog.snapshot()
            parts["node_types"] = sorted(str(name) for name in mapping)
        except Exception:
            parts["node_types"] = []
    if node_records is not None:
        parts["node_types"] = sorted(node_records)
        parts["node_records"] = hashlib.sha256(json.dumps(node_records, sort_keys=True, default=str).encode()).hexdigest()
    parts["chunking"] = chunking
    # Detect edits to existing docs/examples, not just changes to installed node names.
    inputs = {}
    for root in _custom_node_dirs():
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS
                             and not _is_generated_graft_dir(os.path.join(directory, name)))
            example_dir = any(part.lower() in EXAMPLE_DIR_NAMES for part in os.path.relpath(directory, root).split(os.sep))
            for name in sorted(files):
                if not name.lower().endswith(DOC_EXTENSIONS) and not (example_dir and name.lower().endswith(".json")):
                    continue
                path = os.path.join(directory, name)
                if _generated_graft_path(path):
                    continue
                inputs[path] = _stat_stamp(path)
    parts["local_files"] = inputs
    parts["file_limit"] = _limit("kb_max_file_bytes", 0)
    manager = _manager_dir()
    parts["manager"] = {
        name: _stat_stamp(os.path.join(manager, name))
        for name in ("custom-node-list.json", "extension-node-map.json", "model-list.json")
    }
    kb_config = _kb_config() if kb_config is None else kb_config
    embed = kb_config.get("embed", {}) or {}
    parts["flags"] = {
        "examples": bool(kb_config.get("examples", True)),
        "registry": bool(kb_config.get("registry", True)),
        "extended_official": bool(kb_config.get("extended_official", True)),
        "embed_enabled": bool(embed.get("enabled", True)),
        "embed_model": str(embed.get("model") or ""),
    }
    if components is not None:
        components.update({name: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
                           for name, value in parts.items()})
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _embed_settings() -> tuple[dict[str, Any] | None, str]:
    try:
        try:
            from .config_store import CONFIG_STORE
        except ImportError:
            from config_store import CONFIG_STORE
        config = CONFIG_STORE.resolved()
    except Exception:
        return None, ""
    kb_config = config.get("kb", {}) or {}
    embed_config = kb_config.get("embed", {}) or {}
    if not kb_config.get("enabled", True) or not embed_config.get("enabled", True):
        return None, ""
    return config, str(embed_config.get("model") or "").strip()


def _providers_module() -> Any:
    try:
        from . import providers
    except ImportError:
        import providers
    return providers


def _resolve_embed_model(config: Mapping[str, Any], model: str) -> str:
    if model:
        return model
    try:
        return asyncio.run(_providers_module().resolve_embedding_model(config, model))
    except Exception:
        return ""


def index_embeddings(force_rebuild: bool = False) -> int:
    _state["embed_error"] = ""
    config, model = _embed_settings()
    if config is None:
        return 0
    try:
        providers = _providers_module()
    except Exception:
        return 0
    import numpy as np

    model = _resolve_embed_model(config, model)
    if not model:
        _state["embed_error"] = "No embedding model available for the configured provider."
        _state["embed_reason"] = "Embedding model unavailable; existing vectors retained"
        return 0
    _state["embed_model"] = model
    identity_fn = getattr(_providers_module(), "embedding_identity", None)
    identity = identity_fn(config, model) if identity_fn else model
    try:
        window_tokens, budget_chars, window_source = asyncio.run(providers.embedding_limits(config, model))
    except Exception as exc:
        _state["embed_error"] = str(exc)
        _state["embed_reason"] = "Embedding window unavailable; existing vectors retained"
        return 0
    conn = _connect()
    try:
        _init(conn)
        verified_identity = _meta_get("embed_window_verified_identity")
        verified_tokens = int(_meta_get("embed_window_verified_tokens") or 0)
        _state.update(embed_window_observed_tokens=window_tokens,
                      embed_window_observed_source=window_source)
        if window_source == "fallback" and verified_identity == identity and verified_tokens > 0:
            window_tokens = verified_tokens
            window_source = "last verified; fallback ignored"
        elif window_source != "fallback":
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_window_verified_identity', ?)", (identity,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_window_verified_tokens', ?)", (str(window_tokens),))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_window_tokens', ?)", (str(window_tokens),))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_window_source', ?)", (window_source,))
            conn.commit()
        _state["embed_window_tokens"] = window_tokens
        _state["embed_window_source"] = window_source
        previous_identity = _meta_get("embed_identity") or _meta_get("embed_model")
        previous_model = _meta_get("embed_model")
        model_changed = bool(previous_model and previous_model != model)
        vector_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        fragment_count = conn.execute("SELECT COUNT(*) FROM embedding_fragments").fetchone()[0]
        _state.update(embed_current_identity=identity, embed_previous_identity=previous_identity,
                      embed_endpoint_confirmation_required=False)
        if force_rebuild:
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM embedding_fragments")
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (model,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_identity', ?)", (identity,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_input_policy', ?)", ('full-text-v1',))
            conn.commit()
            _vectors_cache.update({"count": -1, "ids": None, "matrix": None})
            vector_count = 0
            model_changed = False
            _state["embed_reason"] = "Manual full vector rebuild requested"
        elif previous_identity and previous_identity != identity and not model_changed:
            if vector_count or fragment_count:
                _state.update(embed_reason="Embedding server changed; compatibility confirmation required",
                              embed_endpoint_confirmation_required=True)
                _state["embed_error"] = "Embedding server changed; confirm reuse or request a full vector rebuild."
                return vector_count
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (model,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_identity', ?)", (identity,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_input_policy', ?)", ('full-text-v1',))
            conn.commit()
            _state["embed_reason"] = "Embedding server changed; indexing missing chunks"
        elif model_changed and vector_count:
            # Stage the replacement model's first successful batch before removing the old space.
            _state["embed_reason"] = f"Model changed; rebuilding vectors for {model}"
        elif not previous_identity:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (model,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_identity', ?)", (identity,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_input_policy', ?)", ('full-text-v1',))
            conn.commit()
            _state["embed_reason"] = ""
        else:
            _state["embed_reason"] = ""
        have = {row[0] for row in conn.execute("SELECT chunk_id FROM embeddings")}
        if model_changed:
            have = set()
        pending = [(row[0], row[1]) for row in conn.execute("SELECT id, content FROM chunks") if row[0] not in have]
        total = len(pending)
        if not total:
            _state["embedded"] = len(have)
            _state["embed_reason"] = "Knowledge base embeddings are up to date"
            return _state["embedded"]
        if model_changed:
            _state["embed_reason"] = f"Model changed; rebuilding all {total} chunks"
        else:
            _state["embed_reason"] = (f"Resuming {total} pending chunks" if have else
                                       f"Updating {total} new or changed chunks")
        _set_progress("Embedding chunks", 0, total)
        done = 0
        for start in range(0, total, EMBED_BATCH):
            batch = pending[start:start + EMBED_BATCH]
            parts = [(chunk_id, part, text)
                     for chunk_id, content in batch
                     for part, text in enumerate(embedding_parts(str(content), window_tokens or 512))
                     if text.strip()]
            try:
                vectors = []
                for offset in range(0, len(parts), EMBED_BATCH):
                    texts = [text for _, _, text in parts[offset:offset + EMBED_BATCH]]
                    received = asyncio.run(providers.embed(config, texts, model))
                    if len(received) != len(texts) or any(not vector for vector in received):
                        raise ValueError("Embedding provider returned an incomplete batch; pending chunks will retry.")
                    vectors.extend(received)
            except Exception as exc:
                _state["embed_error"] = str(exc)
                break
            if len(vectors) != len(parts) or any(not vector for vector in vectors):
                _state["embed_error"] = "Embedding provider returned an incomplete batch; pending chunks will retry."
                break
            rows, fragments = [], []
            seen = set()
            for (chunk_id, part, _), vector in zip(parts, vectors):
                array = np.asarray(vector, dtype="float32")
                if array.ndim != 1 or not np.isfinite(array).all():
                    raise ValueError("Embedding provider returned an invalid vector.")
                row = (int(chunk_id), int(array.shape[0]), array.tobytes())
                if chunk_id not in seen:
                    rows.append(row)
                    seen.add(chunk_id)
                else:
                    fragments.append((row[0], part, row[1], row[2]))
            if len({row[1] for row in rows} | {row[2] for row in fragments}) > 1:
                raise ValueError("Embedding provider returned inconsistent dimensions.")
            if rows:
                if model_changed:
                    conn.execute("DELETE FROM embeddings")
                    conn.execute("DELETE FROM embedding_fragments")
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (model,))
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_identity', ?)", (identity,))
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_input_policy', ?)", ('full-text-v1',))
                    model_changed = False
                conn.executemany("INSERT OR REPLACE INTO embeddings (chunk_id, dim, vec) VALUES (?, ?, ?)", rows)
                conn.executemany("INSERT OR REPLACE INTO embedding_fragments (chunk_id, part, dim, vec) VALUES (?, ?, ?, ?)", fragments)
                conn.commit()
            done += len(rows)
            _set_progress("Embedding chunks", done, total)
        _state["embedded"] = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        if not _state.get("embed_error"):
            _state["embed_reason"] = "Knowledge base embeddings are up to date"
        return _state["embedded"]
    finally:
        conn.close()


def confirm_embedding_compatibility(expected_previous: str, expected_current: str, current_model: str,
                                    current_identity: str, action: str) -> bool:
    """Apply an explicit compatibility choice only while both identities still match."""
    if action not in {"reuse", "rebuild"} or current_identity != expected_current:
        return False
    with _lock:
        if _state.get("building"):
            return False
        conn = _connect()
        try:
            _init(conn)
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT value FROM meta WHERE key = 'embed_identity'").fetchone()
            if not previous:
                previous = conn.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
            stored = previous[0] if previous else ""
            stored_model_row = conn.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
            stored_model = stored_model_row[0] if stored_model_row else ""
            if stored != expected_previous or (action == "reuse" and stored_model != current_model):
                conn.rollback()
                return False
            if action == "rebuild":
                conn.execute("DELETE FROM embeddings")
                conn.execute("DELETE FROM embedding_fragments")
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (current_model,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_identity', ?)", (current_identity,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_input_policy', ?)", ('full-text-v1',))
            conn.commit()
            _vectors_cache.update({"count": -1, "ids": None, "matrix": None})
            _state.update(embed_endpoint_confirmation_required=False, embed_error="",
                          embed_reason=("Embedding server change accepted for the same model; existing vectors reused"
                                        if action == "reuse" else "Manual full vector rebuild requested"))
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


_vectors_cache: dict[str, Any] = {"count": -1, "ids": None, "matrix": None}


def _load_vectors(conn: sqlite3.Connection) -> tuple[Any, Any]:
    import numpy as np

    count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    revision = conn.execute("SELECT value FROM meta WHERE key = 'embeddings_revision'").fetchone()
    cache_key = (os.path.realpath(DB_PATH), revision[0] if revision else "0")
    with _lock:
        if (_vectors_cache.get("key") == cache_key and _vectors_cache["count"] == count
                and _vectors_cache["matrix"] is not None):
            return _vectors_cache["ids"], _vectors_cache["matrix"]
    ids: list[int] = []
    vectors = []
    for chunk_id, _dim, blob in conn.execute(
        "SELECT chunk_id, dim, vec FROM embeddings UNION ALL "
        "SELECT chunk_id, dim, vec FROM embedding_fragments"
    ):
        ids.append(int(chunk_id))
        vectors.append(np.frombuffer(blob, dtype="float32"))
    if not vectors:
        _vectors_cache.update({"count": count, "ids": None, "matrix": None})
        return None, None
    if len({vector.shape[0] for vector in vectors}) != 1:
        # Mixed dimensions (e.g. a changed embedding model slipped through) -> skip vector search.
        _vectors_cache.update({"count": count, "ids": None, "matrix": None})
        return None, None
    matrix = np.vstack(vectors)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    id_array = np.asarray(ids, dtype="int64")
    with _lock:
        _vectors_cache.update({"key": cache_key, "count": count, "ids": id_array, "matrix": matrix})
    return id_array, matrix


def _vector_search(conn: sqlite3.Connection, query: str, limit: int, source: str | None) -> list[tuple[int, dict[str, Any]]]:
    config, model = _embed_settings()
    if config is None:
        return []
    try:
        providers = _providers_module()
    except Exception:
        return []
    import numpy as np

    stored_model = conn.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
    stored_identity = conn.execute("SELECT value FROM meta WHERE key = 'embed_identity'").fetchone()
    identity_fn = getattr(providers, "embedding_identity", None)
    current_identity = identity_fn(config, stored_model[0]) if identity_fn and stored_model else (stored_model[0] if stored_model else "")
    identity_mismatch = bool(stored_identity and stored_identity[0] != current_identity)
    # Legacy indexes created before endpoint identities are safe only with legacy chat routing.
    if not stored_identity and (config.get("embedding") or {}).get("provider"):
        identity_mismatch = True
    if (not stored_model or (model and model != stored_model[0]) or identity_mismatch or not str(query).strip()):
        return []
    model = stored_model[0]
    ids, matrix = _load_vectors(conn)
    if matrix is None:
        return []
    try:
        tokens, _chars, _source = asyncio.run(providers.embedding_limits(config, model))
    except Exception:
        tokens = 512
    try:
        vectors = asyncio.run(providers.embed(config, [part for part in embedding_parts(str(query), tokens) if part.strip()], model))
    except Exception:
        return []
    if not vectors or not vectors[0]:
        return []
    query_vec = np.asarray(vectors, dtype="float32")
    if query_vec.ndim != 2 or matrix.shape[1] != query_vec.shape[1]:
        return []
    query_vec /= np.linalg.norm(query_vec, axis=1, keepdims=True) + 1e-9
    scores = np.full(len(ids), -np.inf)
    for vector in query_vec:
        scores = np.maximum(scores, matrix @ vector)
    order = np.argsort(-scores)
    results: list[tuple[int, dict[str, Any]]] = []
    seen = set()
    for position in order:
        chunk_id = int(ids[position])
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        row = conn.execute(
            "SELECT source_kind, source, pack, path, url, title, heading, content FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if not row:
            continue
        if source and row[0] != source:
            continue
        results.append((chunk_id, _row_to_result(row, query)))
        if limit and len(results) >= limit:
            break
    return results


def _refresh_counts() -> None:
    conn = _connect()
    try:
        conn.execute("BEGIN")
        counts = dict(conn.execute("SELECT source_kind, COUNT(*) FROM chunks GROUP BY source_kind"))
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        values = {"chunks": sum(counts.values()),
                  "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                  "embedded": conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]}
        values.update({f"{kind}_chunks": counts.get(kind, 0)
                       for kind in ("pack", "node", "example", "registry", "model", "official")})
        values.update(embed_model=meta.get("embed_model", ""),
                      embed_identity_stored=meta.get("embed_identity", ""),
                      embed_window_tokens=int(meta.get("embed_window_tokens") or 0),
                      embed_window_source=meta.get("embed_window_source", ""),
                      official_fetched_at=float(meta.get("official_fetched_at") or 0))
        values["official_fetched_iso"] = _iso(values["official_fetched_at"])
        with _lock:
            _state.update(values)
    finally:
        conn.close()


def _freelist_pages() -> int:
    conn = _connect()
    try:
        return int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
    finally:
        conn.close()


def compact(full: bool = True) -> dict[str, Any]:
    before = os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0
    conn = _connect()
    try:
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM" if full else "PRAGMA incremental_vacuum")
    finally:
        conn.close()
    after = os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0
    freed = max(0, before - after)
    _debug_log("kb", "compact", full=full, before_mb=round(before / 1e6, 1), after_mb=round(after / 1e6, 1))
    return {"before_mb": round(before / 1e6, 1), "after_mb": round(after / 1e6, 1), "freed_mb": round(freed / 1e6, 1)}


def _run_extended_docs() -> None:
    try:
        index_extended_official()
    except Exception as exc:
        _state["error"] = f"extended docs: {exc}"
        _meta_set("extended_docs_pending", "1")
    else:
        _meta_set("extended_docs_pending", "0")


def _run_embeddings(force_rebuild: bool = False) -> None:
    _refresh_counts()
    try:
        index_embeddings(force_rebuild=force_rebuild)
    except Exception as exc:
        _state["embed_error"] = str(exc)
    if _state.get("embed_error"):
        _state["error"] = f"embeddings: {_state['embed_error']}"
    _vectors_cache["count"] = -1


def build(force: bool = False, force_official: bool = False, auto_official: bool = True,
          refresh_days: int = 7, delay: float = 0, force_embedding_rebuild: bool = False) -> None:
    if delay:
        time.sleep(delay)
    with _lock:
        if _state["building"]:
            return
        _state["building"] = True
        _state["error"] = ""
        _state["embed_error"] = ""
    started = time.time()
    _debug_log("kb", "build.start", force=force, force_official=force_official, auto_official=auto_official)
    try:
        conn = _connect()
        try:
            _init(conn)
        finally:
            conn.close()

        # Skip the whole rebuild when nothing the indexers depend on has changed. Official docs
        # still follow their own staleness timer.
        _refresh_counts()
        node_records = _collect_node_records()
        chunking = _effective_chunk_chars(refresh=force)
        components = {}
        kb_config = _kb_config()
        fingerprint = _build_fingerprint(node_records, chunking, components, kb_config)
        stored = _meta_get("build_fingerprint")
        if not force and fingerprint and stored == fingerprint:
            if auto_official:
                fetched_at = float(_meta_get("official_fetched_at") or 0)
                stale = (time.time() - fetched_at) > max(1, refresh_days) * 86400
                if force_official or not fetched_at or stale:
                    try:
                        fetch_official(force=True)
                    except Exception as exc:
                        _state["error"] = f"official docs: {exc}"
            if kb_config.get("extended_official", True) and (force_official or _meta_get("extended_docs_pending") == "1"):
                _run_extended_docs()
            # Resume unfinished vectors even when structural inputs are unchanged.
            _run_embeddings(force_rebuild=force_embedding_rebuild)
            _refresh_counts()
            _state["last_build"] = time.time()
            _state["last_build_iso"] = _iso(_state["last_build"])
            _set_progress("Ready")
            _debug_log("kb", "build.skipped", fingerprint=fingerprint, error=_state.get("error") or None)
            return

        try:
            previous_components = json.loads(_meta_get("build_components") or "{}")
        except ValueError:
            previous_components = {}
        changed_components = sorted(name for name, value in components.items() if previous_components.get(name) != value)
        _debug_log("kb", "build.reason", changed=changed_components, reason="forced" if force else "inputs_changed" if stored else "first_build",
                   previous=stored, current=fingerprint, nodes=len(node_records))
        index_local()
        index_node_schemas(node_records, chunking)
        if kb_config.get("examples", True):
            index_examples(chunking)
        if kb_config.get("registry", True):
            index_registry(chunking)
        if auto_official:
            fetched_at = float(_meta_get("official_fetched_at") or 0)
            stale = (time.time() - fetched_at) > max(1, refresh_days) * 86400
            if force_official or not fetched_at or stale:
                try:
                    fetch_official(force=True)
                except Exception as exc:
                    _state["error"] = f"official docs: {exc}"
        if kb_config.get("extended_official", True):
            _run_extended_docs()
        _run_embeddings(force_rebuild=force_embedding_rebuild)
        _refresh_counts()
        try:
            if _freelist_pages() > 5000:
                _set_progress("Compacting")
                compact(full=True)
            else:
                compact(full=False)
        except Exception as exc:
            _debug_log("kb", "compact.error", level="warning", error=str(exc))
        # Structural indexing completed, so record the fingerprint even if docs/embeddings hit
        # a transient error (pending embeddings resume even on the skip path) — otherwise an offline
        # machine would rebuild the whole index on every start.
        _meta_set("build_components", json.dumps(components, sort_keys=True))
        _meta_set("build_fingerprint", fingerprint)
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
    force: bool = False,
    auto_official: bool = True,
    refresh_days: int = 7,
    force_official: bool = False,
    delay: float = 0,
    force_embedding_rebuild: bool = False,
) -> None:
    thread = threading.Thread(
        target=build,
        kwargs={
            "force": force,
            "force_official": force_official,
            "auto_official": auto_official,
            "refresh_days": refresh_days,
            "delay": delay,
            "force_embedding_rebuild": force_embedding_rebuild,
        },
        name="ComfyUIAssistantKB",
        daemon=True,
    )
    thread.start()


def status() -> dict[str, Any]:
    try:
        _refresh_counts()
    except Exception:
        pass
    with _lock:
        result = dict(_state)
    result["coverage"] = node_coverage()
    return result


def node_coverage() -> dict[str, Any]:
    conn = _connect()
    try:
        records = {name: {"schema_error": error, "purpose": {"text": purpose}} for name, error, purpose in conn.execute(
            "SELECT name, json_extract(record, '$.schema_error'), json_extract(record, '$.purpose.text') FROM node_records")}
        try:
            mapping, _ = node_catalog.snapshot(attempts=3, delay=0.05)
        except Exception as exc:
            return {"registry_available": False, "error": str(exc), "indexed": len(records)}
        registered = set(mapping)
        valid = {name for name, record in records.items() if not record.get("schema_error")}
        return {"registry_available": True, "registered": len(registered), "indexed": len(valid & registered),
                "missing": sorted(registered - records.keys()), "unavailable": sorted(records.keys() - registered),
                "failures": [{"name": name, "error": record["schema_error"]} for name, record in records.items()
                             if name in registered and record.get("schema_error")],
                "unknown_purpose": sorted(name for name in valid & registered if not records[name].get("purpose", {}).get("text")),
                "scope": "Nodes registered in this ComfyUI session; disabled or failed-to-load packages are not available nodes."}
    except sqlite3.OperationalError as exc:
        return {"registry_available": False, "error": str(exc), "indexed": 0}
    finally:
        conn.close()


def _snippet(content: str, query: str, length: int = 0) -> str:
    # length <= 0 returns the full chunk (chunks are already size-bounded by the chunker).
    if length and length > 0 and len(content) <= length:
        return content
    if not length or length <= 0:
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


def _fts_rows(conn: sqlite3.Connection, query: str, limit: int, source: str | None) -> list[tuple[int, dict[str, Any]]]:
    match = fts_query(query, min_length=2, limit=0)
    if not match:
        return []
    sql = (
        "SELECT c.id, c.source_kind, c.source, c.pack, c.path, c.url, c.title, c.heading, c.content "
        "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ?"
    )
    params: list[Any] = [match]
    if source:
        sql += " AND c.source_kind = ?"
        params.append(source)
    sql += " ORDER BY bm25(chunks_fts)"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [(int(row[0]), _row_to_result(tuple(row)[1:], query)) for row in conn.execute(sql, params).fetchall()]


def _search_conn(conn: sqlite3.Connection, query: str, limit: int, source: str | None) -> list[dict[str, Any]]:
    return [item for _chunk_id, item in _fts_rows(conn, query, limit, source)]


def search(query: str, limit: int = 0, source: str | None = None) -> list[dict[str, Any]]:
    limit = _wanted(limit)
    conn = _connect()
    try:
        _init(conn)
        ranked = limit * 3 if limit else 0
        fts = _fts_rows(conn, query, ranked, source)
        vectors = _vector_search(conn, query, ranked, source)
        if not vectors:
            return [item for _chunk_id, item in (fts[:limit] if limit else fts)]
        scores: dict[int, float] = {}
        items: dict[int, dict[str, Any]] = {}
        for rank, (chunk_id, item) in enumerate(fts):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            items[chunk_id] = item
        for rank, (chunk_id, item) in enumerate(vectors):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            items.setdefault(chunk_id, item)
        ordered = sorted(scores, key=lambda chunk_id: -scores[chunk_id])
        if limit:
            ordered = ordered[:limit]
        return [items[chunk_id] for chunk_id in ordered]
    finally:
        conn.close()


def node_docs(node_type: str, limit: int = 0) -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        limit = _wanted(limit)
        try:
            live_record = node_catalog.record(node_type)
        except Exception as exc:
            live_record = {"name": node_type, "available": False, "schema_error": str(exc)}
        example_sql = "SELECT path, record FROM node_examples WHERE node_type = ? ORDER BY path"
        example_args: tuple[Any, ...] = (node_type,)
        if limit:
            example_sql += " LIMIT ?"
            example_args = (node_type, limit)
        linked_examples = [{"path": path, **json.loads(value)} for path, value in conn.execute(
            example_sql, example_args)]
        row = conn.execute(
            "SELECT pack FROM chunks WHERE source_kind = 'node' AND source = ? LIMIT 1",
            (node_type,),
        ).fetchone()
        pack = row[0] if row else ""
        results = [
            _row_to_result(item, node_type)
            for item in conn.execute(
                "SELECT source_kind, source, pack, path, url, title, heading, content "
                "FROM chunks WHERE source_kind = 'node' AND source = ?",
                (node_type,),
            ).fetchall()
        ]
        if pack:
            pack_sql = (
                "SELECT source_kind, source, pack, path, url, title, heading, content "
                "FROM chunks WHERE source_kind = 'pack' AND pack = ? AND instr(lower(content), lower(?)) > 0"
            )
            pack_args: list[Any] = [pack, node_type]
            if limit:
                pack_sql += " LIMIT ?"
                pack_args.append(max(1, limit - len(results)))
            results.extend(
                _row_to_result(item, node_type) for item in conn.execute(pack_sql, pack_args).fetchall()
            )
        if not limit or len(results) < limit:
            seen = {(item["source_kind"], item["path"], item["title"], item["heading"]) for item in results}
            for item in _search_conn(conn, node_type, limit, source=None):
                key = (item["source_kind"], item["path"], item["title"], item["heading"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
                if limit and len(results) >= limit:
                    break
        if not pack:
            pack = next((item["pack"] for item in results if item["pack"]), "")
        return {"node_type": node_type, "pack": pack, "node": live_record, "examples": linked_examples,
                "results": results, "verified": bool(live_record.get("available") and not live_record.get("schema_error"))}
    finally:
        conn.close()
