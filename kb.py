from __future__ import annotations

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
except ImportError:
    import node_catalog

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

# Bump when the indexing logic changes so the next start rebuilds once instead of
# skipping on a stale fingerprint.
KB_BUILD_VERSION = 1

EXAMPLE_DIR_NAMES = {"example_workflows", "example_workflow", "examples", "workflows"}
README_URL = "https://raw.githubusercontent.com/comfyanonymous/ComfyUI/master/README.md"
EMBED_BATCH = 32
EMBED_MAX_CHARS = 2000

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
        CREATE TABLE IF NOT EXISTS node_records (name TEXT PRIMARY KEY, record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS node_examples (node_type TEXT, path TEXT, record TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS node_examples_type ON node_examples(node_type);
        CREATE TABLE IF NOT EXISTS embeddings (chunk_id INTEGER PRIMARY KEY, dim INTEGER, vec BLOB);
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
        -- Old vectors may already refer to reused chunk IDs. Rebuild them once.
        DELETE FROM embeddings WHERE NOT EXISTS (
            SELECT 1 FROM meta WHERE key = 'embeddings_cleanup_v1'
        );
        INSERT OR IGNORE INTO meta(key, value) VALUES ('embeddings_cleanup_v1', '1');
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


def _node_info(node_type: str) -> dict[str, Any]:
    return node_catalog.record(node_type)


def _spec_text(spec: Any) -> str:
    if not isinstance(spec, (list, tuple)):
        return str(spec)
    try:
        type_part = spec[0]
        options = spec[1] if len(spec) > 1 else {}
    except (TypeError, IndexError):
        return str(spec)
    if isinstance(type_part, (list, tuple)):
        type_text = "enum(" + ", ".join(str(value) for value in type_part[:40]) + ")"
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


def index_node_schemas() -> int:
    mapping, _ = node_catalog.registry()
    conn = _connect()
    try:
        _init(conn)
        conn.execute("DELETE FROM chunks WHERE source_kind = 'node'")
        conn.execute("DELETE FROM node_records")
        records = []
        total = len(mapping)
        _set_progress("Indexing node schemas", 0, total)
        for index, node_type in enumerate(mapping, start=1):
            try:
                info = _node_info(node_type)
                content = _node_content(info)
            except Exception as exc:
                info = {"name": node_type, "available": True, "schema_error": str(exc)}
                content = ""
            conn.execute("INSERT INTO node_records VALUES (?, ?)", (node_type, json.dumps(info)))
            if info.get("schema_error"):
                continue
            purpose = info.get("purpose", {}).get("text", "")
            if purpose and purpose != info.get("description"):
                content += "\nSource docstring: " + purpose
            module = str(info.get("python_module") or "")
            pack = _pack_from_module(module)
            display = str(info.get("display_name") or node_type)
            category = str(info.get("category") or "")
            records.append(("node", node_type, pack, None, "", display, category, content))
            if index % 200 == 0 or index == total:
                _set_progress("Indexing node schemas", index, total)
        _insert_chunks(conn, records)
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
                values = ", ".join(str(value)[:40] for value in widgets[:3])
                settings.append(f"{node_type}: {values}")
    elif data and all(isinstance(value, dict) and "class_type" in value for value in data.values()):
        for value in data.values():
            node_type = str(value.get("class_type") or "")
            types.append(node_type)
            inputs = value.get("inputs") or {}
            values = ", ".join(f"{key}={str(item)[:30]}" for key, item in list(inputs.items())[:3])
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
        lines.append("Settings: " + " | ".join(settings[:12]))
    return "\n".join(lines)


def index_examples() -> int:
    roots = _custom_node_dirs()
    conn = _connect()
    try:
        _init(conn)
        conn.execute("DELETE FROM chunks WHERE source_kind = 'example'")
        conn.execute("DELETE FROM node_examples")
        records = []
        _set_progress("Indexing example workflows", 0, 0)
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS]
                if not any(part.lower() in EXAMPLE_DIR_NAMES for part in os.path.relpath(dirpath, root).split(os.sep)):
                    continue
                for name in filenames:
                    if not name.lower().endswith(".json"):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        if os.path.getsize(full) > MAX_FILE_BYTES:
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
                    records.append(("example", full, pack, full, "", title, "", summary))
        _insert_chunks(conn, records)
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


def index_registry() -> int:
    manager = _manager_dir()
    if not manager:
        return 0
    conn = _connect()
    try:
        _init(conn)
        conn.execute("DELETE FROM chunks WHERE source_kind IN ('registry', 'model')")
        records = []
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
            records.append(("registry", title or reference, title, None, reference, title, "", content))

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
                records.append(("registry", pack, pack, None, reference, pack, "", content))

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
            records.append(("model", name, "", None, reference, name, "", content))

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


def _fetch_url_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-Assistant-KB"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def index_extended_official() -> int:
    conn = _connect()
    try:
        _init(conn)
        conn.execute("DELETE FROM chunks WHERE source_kind = 'official' AND url LIKE 'ext:%'")
        records = []
        _set_progress("Indexing extended docs", 0, 0)
        try:
            readme = _fetch_url_text(README_URL)
            url = "ext:comfyui-readme"
            for heading, content in _chunks_for_text(_clean_official(readme)):
                records.append(("official", url, "", None, url, "ComfyUI README", heading, content))
        except Exception as exc:
            _debug_log("kb", "readme.fetch_error", level="warning", error=str(exc))
        _insert_chunks(conn, records)
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


def _build_fingerprint() -> str:
    """Cheap signature of everything the indexers depend on.

    Used to skip a rebuild when nothing changed. Deliberately coarse: it reads the installed
    node-type list, the Manager files' stats and the KB flags, not node sources or docs.
    """
    parts: dict[str, Any] = {"version": KB_BUILD_VERSION}
    try:
        mapping, _ = node_catalog.registry()
        parts["node_types"] = sorted(str(name) for name in mapping)
    except Exception:
        parts["node_types"] = []
    manager = _manager_dir()
    parts["manager"] = {
        name: _stat_stamp(os.path.join(manager, name))
        for name in ("custom-node-list.json", "extension-node-map.json", "model-list.json")
    }
    kb_config = _kb_config()
    embed = kb_config.get("embed", {}) or {}
    parts["flags"] = {
        "examples": bool(kb_config.get("examples", True)),
        "registry": bool(kb_config.get("registry", True)),
        "extended_official": bool(kb_config.get("extended_official", True)),
        "embed_enabled": bool(embed.get("enabled", True)),
        "embed_model": str(embed.get("model") or ""),
    }
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
        import asyncio

        candidates = asyncio.run(_providers_module().embedding_models(config))
    except Exception:
        return ""
    if not candidates:
        return ""
    for candidate in candidates:
        if "nomic" in candidate.lower():
            return candidate
    return candidates[0]


def index_embeddings() -> int:
    config, model = _embed_settings()
    if config is None:
        return 0
    try:
        providers = _providers_module()
    except Exception:
        return 0
    import asyncio

    import numpy as np

    model = _resolve_embed_model(config, model)
    if not model:
        _state["embed_error"] = "No embedding model available for the configured provider."
        return 0
    _state["embed_model"] = model

    conn = _connect()
    try:
        _init(conn)
        # If the embedding model changed, the stored vectors are from a different space (and may have
        # a different dimension), so wipe them and re-embed everything.
        previous = _meta_get("embed_model")
        if previous and previous != model:
            conn.execute("DELETE FROM embeddings")
            conn.commit()
            _vectors_cache.update({"count": -1, "ids": None, "matrix": None})
        have = {row[0] for row in conn.execute("SELECT chunk_id FROM embeddings")}
        pending = [(row[0], row[1]) for row in conn.execute("SELECT id, content FROM chunks") if row[0] not in have]
        total = len(pending)
        if not total:
            _state["embedded"] = len(have)
            return _state["embedded"]
        _set_progress("Embedding chunks", 0, total)
        done = 0
        for start in range(0, total, EMBED_BATCH):
            batch = pending[start:start + EMBED_BATCH]
            texts = [str(content)[:EMBED_MAX_CHARS] for _, content in batch]
            try:
                vectors = asyncio.run(providers.embed(config, texts, model))
            except Exception as exc:
                _state["embed_error"] = str(exc)
                break
            rows = []
            for (chunk_id, _), vector in zip(batch, vectors):
                if not vector:
                    continue
                array = np.asarray(vector, dtype="float32")
                rows.append((int(chunk_id), int(array.shape[0]), array.tobytes()))
            if rows:
                conn.executemany("INSERT OR REPLACE INTO embeddings (chunk_id, dim, vec) VALUES (?, ?, ?)", rows)
                conn.commit()
            done += len(batch)
            _set_progress("Embedding chunks", done, total)
        _meta_set("embed_model", model)
        _state["embedded"] = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        return _state["embedded"]
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
    for chunk_id, _dim, blob in conn.execute("SELECT chunk_id, dim, vec FROM embeddings"):
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
    import asyncio

    import numpy as np

    stored_model = conn.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
    if not stored_model or (model and model != stored_model[0]) or not str(query).strip():
        return []
    model = stored_model[0]
    ids, matrix = _load_vectors(conn)
    if matrix is None:
        return []
    try:
        vectors = asyncio.run(providers.embed(config, [query], model))
    except Exception:
        return []
    if not vectors or not vectors[0]:
        return []
    query_vec = np.asarray(vectors[0], dtype="float32")
    if matrix.shape[1] != query_vec.shape[0]:
        return []
    query_vec /= np.linalg.norm(query_vec) + 1e-9
    scores = matrix @ query_vec
    order = np.argsort(-scores)
    results: list[tuple[int, dict[str, Any]]] = []
    for position in order:
        chunk_id = int(ids[position])
        row = conn.execute(
            "SELECT source_kind, source, pack, path, url, title, heading, content FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if not row:
            continue
        if source and row[0] != source:
            continue
        results.append((chunk_id, _row_to_result(row, query)))
        if len(results) >= limit:
            break
    return results


def _refresh_counts() -> None:
    conn = _connect()
    try:
        _init(conn)
        _state["chunks"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        _state["files"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        for kind in ("pack", "node", "example", "registry", "model", "official"):
            _state[f"{kind}_chunks"] = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE source_kind = ?", (kind,)
            ).fetchone()[0]
        _state["embedded"] = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        _state["embed_model"] = _meta_get("embed_model")
        fetched = _meta_get("official_fetched_at")
        _state["official_fetched_at"] = float(fetched) if fetched else 0.0
        _state["official_fetched_iso"] = _iso(_state["official_fetched_at"])
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


def build(force: bool = False, force_official: bool = False, auto_official: bool = True, refresh_days: int = 7, delay: float = 0) -> None:
    if delay:
        time.sleep(delay)
    with _lock:
        if _state["building"]:
            return
        _state["building"] = True
        _state["error"] = ""
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
        fingerprint = _build_fingerprint()
        stored = _meta_get("build_fingerprint")
        if not force and fingerprint and stored == fingerprint:
            fetched = False
            if auto_official:
                fetched_at = float(_meta_get("official_fetched_at") or 0)
                stale = (time.time() - fetched_at) > max(1, refresh_days) * 86400
                if force_official or not fetched_at or stale:
                    try:
                        fetched = fetch_official(force=True)
                    except Exception as exc:
                        _state["error"] = f"official docs: {exc}"
            if fetched:
                # A docs refresh replaced the official chunks, so their vectors are gone. Embed
                # just those; everything else stays untouched.
                try:
                    index_embeddings()
                except Exception as exc:
                    _state["error"] = f"embeddings: {exc}"
                _vectors_cache["count"] = -1
            _refresh_counts()
            _state["last_build"] = time.time()
            _state["last_build_iso"] = _iso(_state["last_build"])
            _set_progress("Ready")
            _debug_log("kb", "build.skipped", fingerprint=fingerprint, error=_state.get("error") or None)
            return

        index_local()
        index_node_schemas()
        kb_config = _kb_config()
        if kb_config.get("examples", True):
            index_examples()
        if kb_config.get("registry", True):
            index_registry()
        if auto_official:
            fetched_at = float(_meta_get("official_fetched_at") or 0)
            stale = (time.time() - fetched_at) > max(1, refresh_days) * 86400
            if force_official or not fetched_at or stale:
                try:
                    fetch_official(force=True)
                except Exception as exc:
                    _state["error"] = f"official docs: {exc}"
        if kb_config.get("extended_official", True):
            try:
                index_extended_official()
            except Exception as exc:
                _state["error"] = f"extended docs: {exc}"
        try:
            index_embeddings()
        except Exception as exc:
            _state["error"] = f"embeddings: {exc}"
        _vectors_cache["count"] = -1
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
        # a transient error (those are retried on their own schedule) — otherwise an offline
        # machine would rebuild the whole index on every start.
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
) -> None:
    thread = threading.Thread(
        target=build,
        kwargs={
            "force": force,
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
        result = dict(_state)
    result["coverage"] = node_coverage()
    return result


def node_coverage() -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        records = {name: {"schema_error": error, "purpose": {"text": purpose}} for name, error, purpose in conn.execute(
            "SELECT name, json_extract(record, '$.schema_error'), json_extract(record, '$.purpose.text') FROM node_records")}
        try:
            mapping, _ = node_catalog.registry()
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
    finally:
        conn.close()


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


def _fts_rows(conn: sqlite3.Connection, query: str, limit: int, source: str | None) -> list[tuple[int, dict[str, Any]]]:
    match = fts_query(query, min_length=2, limit=12)
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
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    params.append(max(1, min(limit, 40)))
    return [(int(row[0]), _row_to_result(tuple(row)[1:], query)) for row in conn.execute(sql, params).fetchall()]


def _search_conn(conn: sqlite3.Connection, query: str, limit: int, source: str | None) -> list[dict[str, Any]]:
    return [item for _chunk_id, item in _fts_rows(conn, query, limit, source)]


def search(query: str, limit: int = 6, source: str | None = None) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        _init(conn)
        ranked = max(limit * 3, 12)
        fts = _fts_rows(conn, query, ranked, source)
        vectors = _vector_search(conn, query, ranked, source)
        if not vectors:
            return [item for _chunk_id, item in fts[:limit]]
        scores: dict[int, float] = {}
        items: dict[int, dict[str, Any]] = {}
        for rank, (chunk_id, item) in enumerate(fts):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            items[chunk_id] = item
        for rank, (chunk_id, item) in enumerate(vectors):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            items.setdefault(chunk_id, item)
        ordered = sorted(scores, key=lambda chunk_id: -scores[chunk_id])[:limit]
        return [items[chunk_id] for chunk_id in ordered]
    finally:
        conn.close()


def node_docs(node_type: str, limit: int = 8) -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        limit = max(1, min(int(limit), 20))
        try:
            live_record = node_catalog.record(node_type)
        except Exception as exc:
            live_record = {"name": node_type, "available": False, "schema_error": str(exc)}
        linked_examples = [{"path": path, **json.loads(value)} for path, value in conn.execute(
            "SELECT path, record FROM node_examples WHERE node_type = ? ORDER BY path LIMIT ?", (node_type, limit))]
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
                    "FROM chunks WHERE source_kind = 'pack' AND pack = ? AND instr(lower(content), lower(?)) > 0 LIMIT ?",
                    (pack, node_type, max(1, limit - len(results))),
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
        return {"node_type": node_type, "pack": pack, "node": live_record, "examples": linked_examples,
                "results": results, "verified": bool(live_record.get("available") and not live_record.get("schema_error"))}
    finally:
        conn.close()
