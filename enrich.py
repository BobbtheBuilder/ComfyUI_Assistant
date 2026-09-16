"""Knowledge enrichment: pack graph, local node facts, per-pack LLM synthesis and web research.

Everything produced here is low-trust evidence (provenance `local:runtime`, `llm:<model>` or
`web:<url>`); only runtime experience can promote knowledge to CONFIRMED.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from typing import Any

try:
    from .debug import debug_log as _debug_log
except ImportError:
    from debug import debug_log as _debug_log

try:
    from . import knowledge
    from .node_catalog import pack_from_module as _pack_from_module
except ImportError:
    import knowledge
    from node_catalog import pack_from_module as _pack_from_module

DOCS_MAX_CHARS = 6000
FETCH_MAX_BYTES = 200000
PACK_BATCH_SIZE = 20
MAX_ATTEMPTS = 3
MAX_LLM_CONFIDENCE = 0.6
MIN_LLM_CONFIDENCE = 0.3
LOCAL_SOURCE = "local:runtime"
LLM_SOURCE = "llm:%s"


def pack_response_format() -> dict[str, Any]:
    """OpenAI-style json_schema request (LM Studio accepts json_schema, not json_object)."""
    node = {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "purpose": {"type": "string"},
            "roles": {"type": "array", "items": {"type": "string"}},
            "architecture_family": {"type": "array", "items": {"type": "string"}},
            "side_effects": {"type": "array", "items": {"type": "string"}},
            "compat": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "op": {"type": "string"},
                    "value": {"type": "array", "items": {"type": "string"}},
                    "generic": {"type": "boolean"},
                },
                "required": ["key"],
            }},
            "confidence": {"type": "number"},
        },
        "required": ["node"],
    }
    schema = {
        "type": "object",
        "properties": {
            "pack": {"type": "object", "properties": {
                "purpose": {"type": "string"},
                "summary": {"type": "string"},
                "side_effects": {"type": "array", "items": {"type": "string"}},
            }},
            "nodes": {"type": "array", "items": node},
        },
        "required": ["pack", "nodes"],
    }
    return {"type": "json_schema",
            "json_schema": {"name": "pack_enrichment", "schema": schema, "strict": False}}


JSON_FORMAT = pack_response_format()

# Compatibility predicates may only reference model properties our evaluator understands.
# architecture_family is deliberately excluded: a family seen in an example must inform, not restrict.
ALLOWED_COMPAT_KEYS = {
    "model_role", "encoder_family", "vae_family", "latent_format",
    "conditioning_format", "task",
}

# Free-text architecture mentions mapped to canonical tokens (longest/most specific first).
ARCHITECTURE_PATTERNS = (
    ("flux2", r"flux\s*\.?\s*2|klein"),
    ("sd35", r"sd\s?3\.?5|sd35"),
    ("sd21", r"sd\s?2\.?1|sd21"),
    ("sd15", r"stable diffusion 1|sd\s?1\.?5|sd15"),
    ("sd3", r"sd\s?3|stable diffusion 3"),
    ("sdxl", r"sd\s?xl|sdxl|stable diffusion xl"),
    ("flux1", r"flux"),
    ("wan", r"\bwan\b"),
    ("hunyuan", r"hunyuan"),
    ("ltxv", r"ltxv|\bltx\b"),
    ("qwen", r"qwen"),
    ("chroma", r"chroma"),
    ("hidream", r"hidream"),
    ("pony", r"\bpony\b"),
    ("illustrious", r"illustrious"),
)


def normalize_architecture(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    for token, pattern in ARCHITECTURE_PATTERNS:
        if re.search(pattern, text):
            return token
    return ""

_lock = threading.RLock()
_state: dict[str, Any] = {"running": False, "phase": "", "error": "", "last_run": 0.0, "processed": 0, "restart": False}
_stop = threading.Event()

_PACK_SYSTEM = (
    "You are a ComfyUI knowledge extractor. For the given custom node pack, describe what the pack "
    "and each of its nodes do, and their compatibility constraints. Use only the supplied evidence; "
    "do not invent. Return JSON only, matching this schema:\n"
    '{"pack": {"purpose": string, "summary": string, "side_effects": [string]},\n'
    ' "nodes": [{"node": string, "purpose": string, "roles": [string], "architecture_family": [string], '
    '"side_effects": [string], "compat": [{"key": string, "op": "in|eq|contains|exists", '
    '"value": [string], "generic": boolean}], "confidence": number}]}\n'
    "roles are generic workflow roles (model_loader, text_encoder_loader, vae_loader, sampler, decoder, "
    "conditioning, latent, scheduler, input, output, control, upscale, utility). compat entries are "
    "requirements over model properties such as model_role, encoder_family, vae_family, latent_format, "
    "architecture_family. Only include nodes that are actually listed."
)


def _now() -> float:
    return time.time()


def _config() -> dict[str, Any]:
    try:
        try:
            from .config_store import CONFIG_STORE
        except ImportError:
            from config_store import CONFIG_STORE
        return CONFIG_STORE.resolved()
    except Exception:
        return {}


def _enrich_config() -> dict[str, Any]:
    return (_config().get("kb", {}) or {}).get("enrich", {}) or {}


def _providers() -> Any:
    try:
        from . import providers
    except ImportError:
        import providers
    return providers


def _kb() -> Any:
    try:
        from . import kb
    except ImportError:
        import kb
    return kb


# -------------------------------------------------------------------- pack metadata

def _manager_dir() -> str:
    try:
        return _kb()._manager_dir()
    except Exception:
        return ""


def _node_repo_map() -> dict[str, str]:
    manager = _manager_dir()
    if not manager:
        return {}
    try:
        with open(os.path.join(manager, "extension-node-map.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, str] = {}
    if isinstance(data, dict):
        for reference, entry in data.items():
            if isinstance(entry, list) and entry and isinstance(entry[0], list):
                for name in entry[0]:
                    result[str(name)] = str(reference)
    return result


def _pack_repo(pack_name: str) -> str:
    conn = knowledge._connect()
    try:
        knowledge._init(conn)
        row = conn.execute("SELECT value FROM properties WHERE entity_id = ? AND key = 'repo' LIMIT 1",
                           (f"pack:{pack_name}",)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def _readme_urls(repo: str) -> list[str]:
    match = re.match(r"https?://github\.com/([^/]+)/([^/#]+)", repo or "")
    if not match:
        return []
    owner, name = match.group(1), match.group(2)
    if name.endswith(".git"):
        name = name[:-4]
    return [f"https://raw.githubusercontent.com/{owner}/{name}/HEAD/README.md",
            f"https://raw.githubusercontent.com/{owner}/{name}/HEAD/readme.md"]


def _fetch_text(url: str) -> str:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-Assistant"})
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed https URLs
            raw = response.read(FETCH_MAX_BYTES)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


# -------------------------------------------------------------------- E1: local facts + queue

def _node_brief(cls: Any, node_type: str, names: dict[str, Any]) -> dict[str, Any]:
    inputs: list[str] = []
    try:
        if callable(getattr(cls, "INPUT_TYPES", None)):
            spec = cls.INPUT_TYPES() or {}
            for group in ("required", "optional"):
                inputs.extend(list((spec.get(group) or {}).keys()))
    except Exception:
        pass
    return {
        "type": node_type,
        "display": names.get(node_type, node_type),
        "category": str(getattr(cls, "CATEGORY", "") or ""),
        "description": str(getattr(cls, "DESCRIPTION", "") or "")[:400],
        "inputs": inputs,
        "outputs": [str(item) for item in (getattr(cls, "RETURN_TYPES", ()) or ())],
    }


def _registry() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        try:
            from . import node_catalog
        except ImportError:
            import node_catalog
        return node_catalog.registry()
    except Exception:
        return {}, {}


def _migrate_llm_quality(conn) -> int:
    """One-time: drop pre-normalization LLM rows and re-queue every pack."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'enrich_quality_v1'").fetchone()
    if row and row[0]:
        return 0
    conn.execute("DELETE FROM requirements WHERE provenance LIKE 'llm:%'")
    conn.execute("DELETE FROM properties WHERE source LIKE 'llm:%'")
    conn.execute("DELETE FROM evidence WHERE source_type = 'llm'")
    conn.execute("UPDATE enrichment SET status = 'pending', attempts = 0, error = ''")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('enrich_quality_v1', '1')")
    conn.commit()
    return 1


def _migrate_quality_v2(conn) -> int:
    """One-time: re-queue every pack after the json_schema/websearch fixes."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'enrich_quality_v2'").fetchone()
    if row and row[0]:
        return 0
    conn.execute("UPDATE enrichment SET status = 'pending', attempts = 0, error = ''")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('enrich_quality_v2', '1')")
    conn.commit()
    return 1


def _migrate_quality_v3(conn) -> int:
    """One-time: drop LLM architecture_family requirements (inform, never restrict)."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'enrich_quality_v3'").fetchone()
    if row and row[0]:
        return 0
    remove = []
    for rid, predicate in conn.execute("SELECT id, predicate FROM requirements WHERE provenance LIKE 'llm:%'").fetchall():
        try:
            if json.loads(predicate).get("key") == "architecture_family":
                remove.append(rid)
        except (TypeError, json.JSONDecodeError):
            continue
    for rid in remove:
        conn.execute("DELETE FROM requirements WHERE id = ?", (rid,))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('enrich_quality_v3', '1')")
    conn.commit()
    return len(remove)


def index_local_facts() -> dict[str, Any]:
    """Store per-node local facts, build the pack graph, and (re)queue packs for enrichment."""
    mapping, names = _registry()
    if not mapping:
        return {"nodes": 0, "packs": 0}
    node_repo = _node_repo_map()
    packs: dict[str, dict[str, Any]] = {}
    conn = knowledge._connect()
    nodes = 0
    try:
        knowledge._init(conn)
        _migrate_llm_quality(conn)
        _migrate_quality_v2(conn)
        _migrate_quality_v3(conn)
        for node_type, cls in mapping.items():
            module = str(getattr(cls, "RELATIVE_PYTHON_MODULE", getattr(cls, "__module__", "")))
            pack = _pack_from_module(module)
            conn.execute(
                "INSERT OR IGNORE INTO entities (id, kind, display, knowledge_type, trust, confidence, provenance, updated_at) "
                "VALUES (?, 'node', ?, 'fact', 'DOCUMENTED', 0.9, ?, ?)",
                (f"node:{node_type}", str(names.get(node_type, node_type)), LOCAL_SOURCE, _now()))
            facts = {
                "pack": pack,
                "category": str(getattr(cls, "CATEGORY", "") or ""),
                "description": str(getattr(cls, "DESCRIPTION", "") or "")[:1000],
                "output_node": "true" if bool(getattr(cls, "OUTPUT_NODE", False)) else "false",
                "source_module": module,
            }
            for key, value in facts.items():
                if value == "":
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
                    "VALUES (?, ?, ?, 0.9, ?, ?)",
                    (f"node:{node_type}", key, str(value), LOCAL_SOURCE, _now()))
            if pack:
                entry = packs.setdefault(pack, {"nodes": [], "repo": ""})
                entry["nodes"].append(node_type)
                if not entry["repo"]:
                    entry["repo"] = node_repo.get(node_type, "")
            nodes += 1
        for pack_name, info in packs.items():
            scope_id = f"pack:{pack_name}"
            conn.execute(
                "INSERT OR IGNORE INTO entities (id, kind, display, knowledge_type, trust, confidence, provenance, updated_at) "
                "VALUES (?, 'pack', ?, 'fact', 'DOCUMENTED', 0.8, ?, ?)",
                (scope_id, pack_name, LOCAL_SOURCE, _now()))
            if info.get("repo"):
                conn.execute(
                    "INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
                    "VALUES (?, 'repo', ?, 0.9, ?, ?)",
                    (scope_id, info["repo"], LOCAL_SOURCE, _now()))
            digest = hashlib.sha256(
                f"{pack_name}|{info.get('repo', '')}|{','.join(sorted(info['nodes']))}".encode()).hexdigest()[:16]
            row = conn.execute("SELECT source_hash, status FROM enrichment WHERE scope_id = ?", (scope_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO enrichment (scope_id, kind, status, source_hash, updated_at) "
                             "VALUES (?, 'pack', 'pending', ?, ?)", (scope_id, digest, _now()))
            elif row[0] != digest:
                conn.execute("UPDATE enrichment SET status = 'pending', source_hash = ?, updated_at = ? "
                             "WHERE scope_id = ?", (digest, _now(), scope_id))
        conn.commit()
    finally:
        conn.close()
    return {"nodes": nodes, "packs": len(packs)}


def _pack_nodes(pack_name: str) -> list[dict[str, Any]]:
    mapping, names = _registry()
    result: list[dict[str, Any]] = []
    for node_type, cls in mapping.items():
        module = str(getattr(cls, "RELATIVE_PYTHON_MODULE", getattr(cls, "__module__", "")))
        if _pack_from_module(module) != pack_name:
            continue
        result.append(_node_brief(cls, node_type, names))
    return result


# -------------------------------------------------------------------- queue / status

def coverage() -> dict[str, Any]:
    conn = knowledge._connect()
    try:
        knowledge._init(conn)
        total = conn.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM enrichment WHERE status = 'done'").fetchone()[0]
        error = conn.execute("SELECT COUNT(*) FROM enrichment WHERE status = 'error'").fetchone()[0]
        skipped = conn.execute("SELECT COUNT(*) FROM enrichment WHERE status = 'skipped'").fetchone()[0]
        running = conn.execute("SELECT COUNT(*) FROM enrichment WHERE status = 'running'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM enrichment WHERE status = 'pending'").fetchone()[0]
        return {"total": total, "done": done, "error": error, "skipped": skipped,
                "pending": pending, "running": running}
    finally:
        conn.close()


def requeue_skipped() -> int:
    conn = knowledge._connect()
    try:
        knowledge._init(conn)
        cur = conn.execute(
            "UPDATE enrichment SET status = 'pending', attempts = 0, error = '', updated_at = ? "
            "WHERE status = 'skipped'", (_now(),))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def _fail_pack(scope_id: str, error: str) -> None:
    max_attempts = max(1, int(_enrich_config().get("max_attempts", MAX_ATTEMPTS) or MAX_ATTEMPTS))
    conn = knowledge._connect()
    try:
        knowledge._init(conn)
        row = conn.execute("SELECT attempts FROM enrichment WHERE scope_id = ?", (scope_id,)).fetchone()
        attempts = int(row[0] or 0) + 1 if row else 1
        status = "skipped" if attempts >= max_attempts else "error"
        conn.execute("UPDATE enrichment SET status = ?, attempts = ?, error = ?, updated_at = ? WHERE scope_id = ?",
                     (status, attempts, error[:500], _now(), scope_id))
        conn.commit()
    finally:
        conn.close()
    _debug_log("enrich", "pack.error", level="warning", scope=scope_id, error=error[:200], status=status)


def _pending_packs(limit: int) -> list[str]:
    conn = knowledge._connect()
    try:
        knowledge._init(conn)
        return [row[0] for row in conn.execute(
            "SELECT scope_id FROM enrichment WHERE kind = 'pack' AND status IN ('pending', 'error') "
            "ORDER BY updated_at LIMIT ?", (max(1, int(limit)),))]
    finally:
        conn.close()


def _set_status(scope_id: str, status: str, model: str = "", error: str = "") -> None:
    conn = knowledge._connect()
    try:
        knowledge._init(conn)
        conn.execute(
            "UPDATE enrichment SET status = ?, model = COALESCE(NULLIF(?, ''), model), "
            "error = ?, updated_at = ? WHERE scope_id = ?",
            (status, model, error[:500], _now(), scope_id))
        conn.commit()
    finally:
        conn.close()


# -------------------------------------------------------------------- docs / web

def _pack_docs(pack_name: str) -> str:
    try:
        results = _kb().search(pack_name, 6, "pack", pack_name)
    except Exception:
        return ""
    return "\n\n".join(str(item.get("snippet") or "") for item in results)[:DOCS_MAX_CHARS]


def _web_research(pack_name: str, repo: str) -> str:
    cfg = _enrich_config()
    parts: list[str] = []
    for url in _readme_urls(repo):
        text = _fetch_text(url)
        if text:
            parts.append(f"# {url}\n{text[:3000]}")
            break
    if cfg.get("web", True):
        try:
            try:
                from . import websearch
            except ImportError:
                import websearch
            results = asyncio.run(websearch.search(_config(), f"{pack_name} ComfyUI node pack",
                                                   int(cfg.get("max_web_queries_per_pack", 3))))
            if results:
                parts.append("Web results:\n" + json.dumps(results)[:3000])
        except Exception as exc:
            _debug_log("enrich", "web.error", level="warning", pack=pack_name, error=str(exc))
    return "\n\n".join(parts)


def _pack_messages(pack_name: str, repo: str, nodes: list[dict[str, Any]], evidence: str) -> list[dict[str, str]]:
    payload = {
        "pack": pack_name,
        "repo": repo,
        "evidence": evidence[:DOCS_MAX_CHARS],
        "nodes": nodes,
    }
    return [
        {"role": "system", "content": _PACK_SYSTEM},
        {"role": "user", "content": json.dumps(payload) + "\n\nReturn JSON only."},
    ]


def _complete(messages: list[dict[str, str]]) -> str:
    config = dict(_config())
    cfg = _enrich_config()
    max_tokens = int(cfg.get("max_tokens", 0) or 0) or None
    config["max_tokens"] = max_tokens or 0
    response_format = None if str(cfg.get("json_mode", "schema")).lower() == "none" else JSON_FORMAT
    try:
        return asyncio.run(_providers().complete(config, messages, max_tokens, response_format))
    except RuntimeError as exc:
        # Some providers reject response_format; retry plainly before failing the pack.
        if response_format is not None and "response_format" in str(exc):
            _debug_log("enrich", "json_mode.fallback", level="warning", error=str(exc)[:200])
            return asyncio.run(_providers().complete(config, messages, max_tokens, None))
        raise


def _parse_json(text: str) -> dict[str, Any] | None:
    value = str(text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", value, re.IGNORECASE)
    if fence:
        value = fence.group(1).strip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(value[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# -------------------------------------------------------------------- E2: apply LLM result

def _clear_node_llm(conn, entity_id: str) -> None:
    node_type = entity_id.split(":", 1)[1] if entity_id.startswith("node:") else entity_id
    conn.execute("DELETE FROM properties WHERE entity_id = ? AND source LIKE 'llm:%'", (entity_id,))
    conn.execute("DELETE FROM requirements WHERE scope_kind = 'node' AND scope_id = ? AND provenance LIKE 'llm:%'",
                 (node_type,))
    conn.execute("DELETE FROM evidence WHERE entity = ? AND source LIKE 'llm:%'", (entity_id,))


def _apply_pack_result(pack_name: str, result: dict[str, Any], model: str) -> int:
    source = LLM_SOURCE % (model or "unknown")
    conn = knowledge._connect()
    applied = 0
    try:
        knowledge._init(conn)
        pack_entity = f"pack:{pack_name}"
        pack = result.get("pack") or {}
        for key, value in (("purpose", pack.get("purpose")), ("enrich_summary", pack.get("summary"))):
            if value:
                conn.execute("INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
                             "VALUES (?, ?, ?, 0.6, ?, ?)", (pack_entity, key, str(value)[:2000], source, _now()))
        for node in result.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("node") or "").strip()
            if not node_type:
                continue
            entity_id = f"node:{node_type}"
            if not conn.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone():
                continue
            _clear_node_llm(conn, entity_id)
            confidence = max(MIN_LLM_CONFIDENCE, min(MAX_LLM_CONFIDENCE, float(node.get("confidence", 0.5) or 0.5)))
            extra_side_effects: list[str] = []
            for key in ("purpose", "architecture_family", "side_effects"):
                raw = node.get(key)
                values = raw if isinstance(raw, list) else ([raw] if raw else [])
                for value in values:
                    if value in (None, ""):
                        continue
                    if key == "architecture_family":
                        value = normalize_architecture(value)
                        if not value:
                            continue
                    conn.execute("INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", (entity_id, key, str(value)[:500], confidence, source, _now()))
            for role in node.get("roles") or []:
                if role:
                    conn.execute("INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
                                 "VALUES (?, 'roles', ?, ?, ?, ?)", (entity_id, str(role)[:80].strip().lower(), confidence, source, _now()))
            for compat in node.get("compat") or []:
                if not isinstance(compat, dict) or not compat.get("key"):
                    continue
                key = str(compat["key"]).strip().lower()
                if key == "architecture_family":
                    # Informational only: already recorded as a node property, never a restriction.
                    continue
                op = str(compat.get("op") or "in").strip().lower()
                raw_values = compat.get("value")
                values = raw_values if isinstance(raw_values, list) else ([raw_values] if raw_values else [])
                values = [str(v).strip().lower() for v in values if v not in (None, "")]
                predicate = {"key": key, "op": op, "value": values, "required": True, "generic": bool(compat.get("generic"))}
                invalid = knowledge.requirement_advisory_reason(predicate, 1.0, "validation:structure")
                if key not in ALLOWED_COMPAT_KEYS or (not values and op != "exists") or invalid:
                    # Not a model-property constraint we can evaluate; keep it as a note instead.
                    note = f"{key} {op} {values}".strip()
                    if note:
                        extra_side_effects.append(f"requires {note}")
                    continue
                conn.execute("INSERT INTO requirements (scope_kind, scope_id, task, predicate, state_match, "
                             "state_mismatch, confidence, provenance, updated_at) "
                             "VALUES ('node', ?, '', ?, 'INFERRED_COMPATIBLE', 'INFERRED_INCOMPATIBLE', ?, ?, ?)",
                             (node_type, json.dumps(predicate), confidence, source, _now()))
            for note in extra_side_effects:
                conn.execute("INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
                             "VALUES (?, 'side_effects', ?, ?, ?, ?)", (entity_id, note[:300], confidence, source, _now()))
            conn.execute("INSERT INTO evidence (claim, source_type, source, entity, version, result, confidence, trust, retrieved_at) "
                         "VALUES (?, 'llm', ?, ?, '', 'extracted', ?, 'INFERRED', ?)",
                         (f"{node_type}: {str(node.get('purpose') or '')[:300]}", source, entity_id, confidence, _now()))
            applied += 1
        conn.commit()
    finally:
        conn.close()
    return applied


def _store_web_evidence(pack_name: str, text: str) -> None:
    if not text or not text.strip():
        return
    conn = knowledge._connect()
    try:
        knowledge._init(conn)
        entity = f"pack:{pack_name}"
        conn.execute("DELETE FROM evidence WHERE entity = ? AND source_type = 'web'", (entity,))
        conn.execute(
            "INSERT INTO evidence (claim, source_type, source, entity, version, result, confidence, trust, retrieved_at) "
            "VALUES (?, 'web', 'web:research', ?, '', 'retrieved', 0.4, 'WEB_UNVERIFIED', ?)",
            (text[:2000], entity, _now()))
        conn.commit()
    finally:
        conn.close()


def enrich_pack(scope_id: str, web: bool = True) -> dict[str, Any]:
    pack_name = scope_id.split(":", 1)[1] if scope_id.startswith("pack:") else scope_id
    nodes = _pack_nodes(pack_name)
    if not nodes:
        _set_status(scope_id, "done")
        return {"nodes": 0}
    cfg = _enrich_config()
    batch_size = max(1, int(cfg.get("batch_size", PACK_BATCH_SIZE) or PACK_BATCH_SIZE))
    evidence = _pack_docs(pack_name)
    repo = _pack_repo(pack_name)
    if web:
        web_text = _web_research(pack_name, repo)
        evidence += "\n\n" + web_text
        _store_web_evidence(pack_name, web_text)
    model = str(cfg.get("model") or _config().get("model") or "")
    batches = [nodes[index:index + batch_size] for index in range(0, len(nodes), batch_size)]
    applied = 0
    for batch in batches:
        result = _parse_json(_complete(_pack_messages(pack_name, repo, batch, evidence)))
        if not result:
            raise ValueError("model returned no valid JSON")
        applied += _apply_pack_result(pack_name, result, model)
    _set_status(scope_id, "done", model=model)
    return {"nodes": applied, "batches": len(batches)}


def _kb_busy() -> bool:
    try:
        return bool(_kb()._state.get("building"))
    except Exception:
        return False


def _chat_busy() -> bool:
    try:
        return _providers().active_chats() > 0
    except Exception:
        return False


def busy_reason() -> str:
    """Empty when enrichment may run; otherwise why it must wait."""
    cfg = _enrich_config()
    if cfg.get("enabled") is False:
        return "disabled"
    if cfg.get("idle_only", True):
        if _kb_busy():
            return "index build"
        if _chat_busy():
            return "chat"
    return ""


def stop() -> None:
    _stop.set()


def run(limit: int = 1, web: bool = True, ignore_busy: bool = False) -> dict[str, Any]:
    cfg = _enrich_config()
    if cfg.get("enabled") is False:
        return {"skipped": True, "reason": "enrichment disabled"}
    if not str(cfg.get("model") or _config().get("model") or ""):
        return {"skipped": True, "reason": "no model configured"}
    processed: list[dict[str, Any]] = []
    for scope_id in _pending_packs(limit):
        if _stop.is_set():
            break
        if not ignore_busy:
            reason = busy_reason()
            if reason:
                with _lock:
                    _state["phase"] = f"waiting: {reason}"
                return {"skipped": True, "reason": reason, "processed": processed, "coverage": coverage()}
        try:
            _set_status(scope_id, "running")
            result = enrich_pack(scope_id, web=web)
            processed.append({"scope": scope_id, "nodes": result.get("nodes", 0)})
        except Exception as exc:
            _fail_pack(scope_id, str(exc))
    with _lock:
        _state["last_run"] = _now()
        _state["processed"] = len(processed)
    return {"processed": processed, "coverage": coverage()}


def start_worker(interval: float | None = None, web: bool = True) -> None:
    """Background worker: one pack per cycle, pausing while the KB builds or a chat is running."""
    with _lock:
        if _state["running"]:
            _state["restart"] = True
            return
        _state["running"] = True
        _state["restart"] = False
        _state["phase"] = "starting"
    _stop.clear()
    cfg = _enrich_config()
    delay = float(interval if interval is not None else cfg.get("interval_seconds", 5) or 5)

    def worker() -> None:
        try:
            while not _stop.is_set():
                if coverage().get("pending", 0) <= 0:
                    with _lock:
                        _state["phase"] = "idle"
                    break
                reason = busy_reason()
                if reason and reason != "disabled":
                    with _lock:
                        _state["phase"] = f"waiting: {reason}"
                    time.sleep(delay)
                    continue
                if reason == "disabled":
                    break
                with _lock:
                    _state["phase"] = "running"
                result = run(limit=1, web=web)
                if result.get("processed"):
                    time.sleep(delay)
        finally:
            with _lock:
                _state["running"] = False
                if _state["phase"] == "running":
                    _state["phase"] = "idle"
                restart = _state.pop("restart", False)
            if restart and not _stop.is_set():
                start_worker(interval=delay, web=web)

    threading.Thread(target=worker, name="ComfyUIAssistantEnrich", daemon=True).start()


def start_background(limit: int = 1, web: bool = True) -> None:
    """Compatibility wrapper: start the idle worker (interval/limit come from config)."""
    start_worker(web=web)


def research_entity(entity_id: str, web: bool = True) -> dict[str, Any]:
    """On-demand, targeted research for a model/pack/node entity (used by the controller)."""
    entity_id = str(entity_id or "").strip()
    if entity_id.startswith("pack:"):
        try:
            return {"ok": True, **enrich_pack(entity_id, web=web)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "research currently targets packs"}


def status() -> dict[str, Any]:
    with _lock:
        snapshot = dict(_state)
    try:
        snapshot["waiting_for"] = busy_reason()
        snapshot["coverage"] = coverage()
    except Exception:
        pass
    return snapshot
