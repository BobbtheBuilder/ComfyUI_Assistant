"""Open-world compatibility knowledge: generic entities, properties, requirements and edges.

Nothing here is architecture-specific. Models, nodes and components are entities with
arbitrary key/value properties; nodes/roles/architectures/tasks carry requirement
predicates over those properties. A model absent from every curated list is still
evaluated against the requirements, so "not listed" never means "incompatible".
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
import time
from typing import Any, Mapping

try:
    from .debug import debug_log as _debug_log
except ImportError:
    from debug import debug_log as _debug_log

try:
    from .storage import connect, schema_version as _schema_version
except ImportError:
    from storage import connect, schema_version as _schema_version

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(NODE_DIR, "assistant_knowledge.sqlite")
MANIFEST_DIR = os.path.join(NODE_DIR, "manifests")

SCHEMA_VERSION = 1

CONFIRMED_COMPATIBLE = "CONFIRMED_COMPATIBLE"
INFERRED_COMPATIBLE = "INFERRED_COMPATIBLE"
GENERIC = "GENERIC"
UNKNOWN = "UNKNOWN"
INFERRED_INCOMPATIBLE = "INFERRED_INCOMPATIBLE"
CONFIRMED_INCOMPATIBLE = "CONFIRMED_INCOMPATIBLE"

STATES = (CONFIRMED_COMPATIBLE, INFERRED_COMPATIBLE, GENERIC, UNKNOWN,
          INFERRED_INCOMPATIBLE, CONFIRMED_INCOMPATIBLE)
# Auto-building may use these; UNKNOWN must be resolved or reported, never assumed.
# States usable for building. UNKNOWN is usable but flagged unverified (warn).
ALLOWED_STATES = frozenset({CONFIRMED_COMPATIBLE, INFERRED_COMPATIBLE, GENERIC})
WARN_STATES = frozenset({UNKNOWN})
BLOCKED_STATES = frozenset({INFERRED_INCOMPATIBLE, CONFIRMED_INCOMPATIBLE})

REQUIREMENT_SCOPES = ("node", "role", "architecture", "task")

# Trust ladder for provenance; recommendations need stronger provenance than facts.
TRUST_ORDER = ("UNVERIFIED", "DOCUMENTED", "OBSERVED", "TESTED", "USER_APPROVED")
PROMOTE_OBSERVED = 2
PROMOTE_TESTED = 4
PROMOTE_CONFIRMED = 3
MAX_FAILURE_RATIO = 0.34

# Only trusted property rows may allow or block a node. Web/low-confidence claims are evidence,
# never a hard decision.
MIN_HARD_CONFIDENCE = 0.5

# The evidence packet must stay small; the controller never dumps every installed node.

# Generic folder -> role mapping. Roles are data, not per-architecture columns.
ROLE_BY_FOLDER = {
    "checkpoints": "checkpoint",
    "diffusion_models": "diffusion_model",
    "unet": "diffusion_model",
    "text_encoders": "text_encoder",
    "clip": "text_encoder",
    "vae": "vae",
    "loras": "lora",
    "controlnet": "controlnet",
    "clip_vision": "clip_vision",
    "style_models": "style_model",
    "embeddings": "embedding",
    "upscale_models": "upscale_model",
}

_lock = threading.RLock()
_state: dict[str, Any] = {"last_index": 0.0, "last_index_iso": "", "error": "", "indexed": 0}

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        display TEXT,
        knowledge_type TEXT DEFAULT 'fact',
        trust TEXT DEFAULT 'UNVERIFIED',
        confidence REAL DEFAULT 0.5,
        provenance TEXT,
        updated_at REAL
    );
    CREATE TABLE IF NOT EXISTS properties (
        entity_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        confidence REAL DEFAULT 0.5,
        source TEXT,
        updated_at REAL,
        PRIMARY KEY (entity_id, key, value)
    );
    CREATE INDEX IF NOT EXISTS properties_key ON properties(key);
    CREATE TABLE IF NOT EXISTS aliases (
        alias TEXT NOT NULL,
        alias_norm TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        kind TEXT,
        source TEXT,
        confidence REAL DEFAULT 0.5,
        PRIMARY KEY (alias_norm, entity_id)
    );
    CREATE INDEX IF NOT EXISTS aliases_norm ON aliases(alias_norm);
    CREATE TABLE IF NOT EXISTS requirements (
        id INTEGER PRIMARY KEY,
        scope_kind TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        task TEXT,
        predicate TEXT NOT NULL,
        state_match TEXT DEFAULT 'INFERRED_COMPATIBLE',
        state_mismatch TEXT DEFAULT 'INFERRED_INCOMPATIBLE',
        confidence REAL DEFAULT 0.5,
        provenance TEXT,
        updated_at REAL
    );
    CREATE INDEX IF NOT EXISTS requirements_scope ON requirements(scope_kind, scope_id);
    CREATE TABLE IF NOT EXISTS edges (
        id INTEGER PRIMARY KEY,
        subject TEXT NOT NULL,
        relation TEXT NOT NULL,
        object TEXT NOT NULL,
        state TEXT NOT NULL,
        confidence REAL DEFAULT 0.5,
        version TEXT,
        provenance TEXT,
        updated_at REAL,
        UNIQUE(subject, relation, object, version)
    );
    CREATE INDEX IF NOT EXISTS edges_subject ON edges(subject, relation);
    CREATE TABLE IF NOT EXISTS assets (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        path TEXT,
        role TEXT,
        size INTEGER,
        mtime REAL,
        sha256 TEXT,
        entity_id TEXT,
        source TEXT,
        updated_at REAL
    );
    CREATE INDEX IF NOT EXISTS assets_entity ON assets(entity_id);
    CREATE INDEX IF NOT EXISTS assets_role ON assets(role);
    CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY,
        claim TEXT,
        source_type TEXT,
        source TEXT,
        entity TEXT,
        version TEXT,
        result TEXT,
        confidence REAL DEFAULT 0.5,
        trust TEXT DEFAULT 'UNVERIFIED',
        retrieved_at REAL
    );
    CREATE TABLE IF NOT EXISTS workflows (
        pattern_id TEXT PRIMARY KEY,
        model_entity TEXT,
        task TEXT,
        nodes TEXT,
        trust TEXT DEFAULT 'UNVERIFIED',
        provenance TEXT,
        updated_at REAL
    );
    CREATE INDEX IF NOT EXISTS workflows_model ON workflows(model_entity, task);
    CREATE TABLE IF NOT EXISTS experiences (
        id INTEGER PRIMARY KEY,
        pattern_id TEXT,
        model_entity TEXT,
        task TEXT,
        outcome TEXT,
        error_sig TEXT,
        nodes TEXT,
        config_hash TEXT,
        source TEXT,
        created_at REAL
    );
    CREATE INDEX IF NOT EXISTS experiences_model ON experiences(model_entity, task);
    CREATE TABLE IF NOT EXISTS enrichment (
        scope_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0,
        source_hash TEXT,
        model TEXT,
        error TEXT,
        updated_at REAL
    );
    CREATE INDEX IF NOT EXISTS enrichment_status ON enrichment(status);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_DROP_SQL = """
    DROP TABLE IF EXISTS enrichment;
    DROP TABLE IF EXISTS experiences;
    DROP TABLE IF EXISTS workflows;
    DROP TABLE IF EXISTS evidence;
    DROP TABLE IF EXISTS assets;
    DROP TABLE IF EXISTS edges;
    DROP TABLE IF EXISTS requirements;
    DROP TABLE IF EXISTS aliases;
    DROP TABLE IF EXISTS properties;
    DROP TABLE IF EXISTS entities;
    DROP TABLE IF EXISTS meta;
"""


def _now() -> float:
    return time.time()


def _connect() -> sqlite3.Connection:
    return connect(DB_PATH)


def _init(conn: sqlite3.Connection) -> None:
    current = _schema_version(conn)
    if current and current != SCHEMA_VERSION:
        conn.executescript(_DROP_SQL)
        _debug_log("knowledge", "schema.rebuild", previous=current, current=SCHEMA_VERSION)
        current = 0
    conn.executescript(_SCHEMA_SQL)
    _migrate(conn)
    if current != SCHEMA_VERSION:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent migrations that preserve learned data. Never raises."""
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence)")}
    except sqlite3.Error:
        columns = set()
    if columns and "entity" not in columns:
        try:
            conn.execute("ALTER TABLE evidence ADD COLUMN entity TEXT")
            conn.commit()
        except sqlite3.Error as exc:
            _debug_log("knowledge", "migrate.error", level="warning", table="evidence", error=str(exc))
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS evidence_entity ON evidence(entity)")
        conn.commit()
    except sqlite3.Error as exc:
        _debug_log("knowledge", "migrate.error", level="warning", target="evidence_entity", error=str(exc))
    try:
        experience_columns = {row[1] for row in conn.execute("PRAGMA table_info(experiences)")}
    except sqlite3.Error:
        experience_columns = set()
    if experience_columns and "config_hash" not in experience_columns:
        try:
            conn.execute("ALTER TABLE experiences ADD COLUMN config_hash TEXT")
            conn.commit()
        except sqlite3.Error as exc:
            _debug_log("knowledge", "migrate.error", level="warning", table="experiences", error=str(exc))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()


# ---------------------------------------------------------------- writes

def add_entity(entity_id: str, kind: str, display: str = "", knowledge_type: str = "fact",
               trust: str = "UNVERIFIED", confidence: float = 0.5, provenance: str = "") -> None:
    conn = _connect()
    try:
        _init(conn)
        conn.execute(
            "INSERT INTO entities (id, kind, display, knowledge_type, trust, confidence, provenance, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, display=excluded.display, "
            "trust=excluded.trust, confidence=excluded.confidence, provenance=excluded.provenance, "
            "updated_at=excluded.updated_at",
            (entity_id, kind, display or entity_id, knowledge_type, trust, float(confidence), provenance, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def set_property(entity_id: str, key: str, value: Any, confidence: float = 0.5, source: str = "") -> None:
    if value is None or value == "":
        return
    conn = _connect()
    try:
        _init(conn)
        conn.execute(
            "INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entity_id, str(key), str(value), float(confidence), source, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def add_requirement(scope_kind: str, scope_id: str, predicate: Mapping[str, Any], task: str = "",
                    state_match: str = INFERRED_COMPATIBLE, state_mismatch: str = INFERRED_INCOMPATIBLE,
                    confidence: float = 0.5, provenance: str = "") -> None:
    if scope_kind not in REQUIREMENT_SCOPES or not scope_id:
        raise ValueError(f"invalid requirement scope: {scope_kind}")
    conn = _connect()
    try:
        _init(conn)
        conn.execute(
            "INSERT INTO requirements (scope_kind, scope_id, task, predicate, state_match, state_mismatch, "
            "confidence, provenance, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scope_kind, scope_id, task, json.dumps(dict(predicate)), state_match, state_mismatch,
             float(confidence), provenance, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def record_experience(outcome: str, model_entity: str = "", task: str = "", error_sig: str = "",
                      nodes: list[str] | None = None, pattern_id: str = "", source: str = "runtime",
                      config_hash: str = "") -> dict[str, Any]:
    outcome = "success" if str(outcome).lower() in ("success", "ok", "completed") else "failure"
    conn = _connect()
    try:
        _init(conn)
        conn.execute(
            "INSERT INTO experiences (pattern_id, model_entity, task, outcome, error_sig, nodes, config_hash, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pattern_id, model_entity, task, outcome, error_sig[:2000], json.dumps(list(nodes or [])),
             config_hash, source, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    promotion: dict[str, Any] = {}
    if outcome == "success" and model_entity:
        try:
            promotion = promote_from_experience(model_entity, task)
        except Exception as exc:  # promotion must never break outcome recording
            _debug_log("knowledge", "promote.error", level="warning", error=str(exc))
    return {"ok": True, "outcome": outcome, "promotion": promotion}


def experience_summary(model_entity: str = "", task: str = "") -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        sql = "SELECT outcome, COUNT(*) FROM experiences"
        params: list[Any] = []
        clauses = []
        if model_entity:
            clauses.append("model_entity = ?")
            params.append(model_entity)
        if task:
            clauses.append("task = ?")
            params.append(task)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY outcome"
        counts = {row[0]: int(row[1]) for row in conn.execute(sql, params)}
        successes, failures = counts.get("success", 0), counts.get("failure", 0)
        return {"successes": successes, "failures": failures, "runs": successes + failures}
    finally:
        conn.close()


def recall_errors(signature: str, limit: int = 5) -> list[dict[str, Any]]:
    """Past failures whose error text shares tokens with the current error (best-effort)."""
    tokens = [token for token in _normalize(signature).split() if len(token) >= 4][:8]
    conn = _connect()
    try:
        _init(conn)
        rows = conn.execute(
            "SELECT error_sig, model_entity, task, nodes, created_at FROM experiences "
            "WHERE outcome = 'failure' ORDER BY created_at DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    scored: list[tuple[int, dict[str, Any]]] = []
    for error_sig, model_entity, task, nodes, _created in rows:
        haystack = _normalize(error_sig or "")
        score = sum(1 for token in tokens if token in haystack)
        if tokens and not score:
            continue
        try:
            node_list = json.loads(nodes or "[]")
        except (TypeError, json.JSONDecodeError):
            node_list = []
        scored.append((score, {"error": error_sig, "model": model_entity, "task": task, "nodes": node_list}))
    scored.sort(key=lambda item: -item[0])
    return [item[1] for item in scored[: max(1, int(limit))]]


def _trust_rank(trust: str) -> int:
    return TRUST_ORDER.index(trust) if trust in TRUST_ORDER else 0


def promote_from_experience(model_entity: str = "", task: str = "") -> dict[str, Any]:
    """Runtime outcomes teach the KB, per exact pattern+config (never by aggregate model/task).

    Only successes that carry a pattern_id qualify. Promotion is monotonic; failures block it.
    """
    if not model_entity:
        return {"promoted": False, "reason": "no target model"}
    conn = _connect()
    try:
        _init(conn)
        sql = "SELECT outcome, nodes, pattern_id, config_hash FROM experiences"
        params: list[Any] = []
        clauses = []
        if model_entity:
            clauses.append("model_entity = ?")
            params.append(model_entity)
        if task:
            clauses.append("task = ?")
            params.append(task)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for outcome, nodes_json, pattern_id, config_hash in conn.execute(sql, params):
            key = (str(pattern_id or ""), str(config_hash or ""))
            group = groups.setdefault(key, {"successes": 0, "failures": 0, "nodes": {}})
            if outcome == "success":
                group["successes"] += 1
                try:
                    for item in json.loads(nodes_json or "[]"):
                        name = str(item)
                        group["nodes"][name] = group["nodes"].get(name, 0) + 1
                except (TypeError, json.JSONDecodeError):
                    continue
            else:
                group["failures"] += 1
        qualifying = [(pid, cfg, group) for (pid, cfg), group in groups.items() if pid]
        qualifying = [(pid, cfg, group) for pid, cfg, group in qualifying
                      if group["successes"] >= PROMOTE_OBSERVED
                      and (group["successes"] + group["failures"]) > 0
                      and (group["failures"] / (group["successes"] + group["failures"])) <= MAX_FAILURE_RATIO]
        if not qualifying:
            return {"promoted": False, "reason": "no pattern+config with consistent successes",
                    "groups": len(groups)}
        node_counts: dict[str, int] = {}
        for _pid, _cfg, group in qualifying:
            for name, count in group["nodes"].items():
                node_counts[name] = node_counts.get(name, 0) + count
        promoted_nodes: list[str] = []
        for node_type, count in node_counts.items():
            if count < PROMOTE_OBSERVED:
                continue
            node_id = f"node:{node_type}"
            row = conn.execute(
                "SELECT id, state FROM edges WHERE subject = ? AND relation = 'compatible_with' AND object = ? "
                "AND (version IS NULL OR version = '')", (model_entity, node_id)).fetchone()
            target = CONFIRMED_COMPATIBLE if count >= PROMOTE_CONFIRMED else INFERRED_COMPATIBLE
            if row is None:
                conn.execute(
                    "INSERT OR IGNORE INTO edges (subject, relation, object, state, confidence, version, provenance, updated_at) "
                    "VALUES (?, 'compatible_with', ?, ?, 0.6, '', 'experience', ?)",
                    (model_entity, node_id, INFERRED_COMPATIBLE, _now()))
                promoted_nodes.append(node_type)
            elif row[1] == INFERRED_COMPATIBLE and target == CONFIRMED_COMPATIBLE:
                conn.execute("UPDATE edges SET state = ?, confidence = 0.8, provenance = 'experience', updated_at = ? WHERE id = ?",
                             (CONFIRMED_COMPATIBLE, _now(), row[0]))
                promoted_nodes.append(node_type)
        promoted_patterns: list[str] = []
        for pid, _cfg, group in qualifying:
            successes, failures = group["successes"], group["failures"]
            if successes >= PROMOTE_TESTED and failures == 0:
                target_trust = "TESTED"
            elif successes >= PROMOTE_OBSERVED:
                target_trust = "OBSERVED"
            else:
                continue
            row = conn.execute("SELECT trust FROM workflows WHERE pattern_id = ?", (pid,)).fetchone()
            if not row:
                continue
            current = row[0] or "UNVERIFIED"
            if (target_trust != current and _trust_rank(target_trust) > _trust_rank(current)
                    and current != "USER_APPROVED"):
                conn.execute("UPDATE workflows SET trust = ?, updated_at = ? WHERE pattern_id = ?",
                             (target_trust, _now(), pid))
                promoted_patterns.append(pid)
        conn.commit()
        return {"promoted": bool(promoted_nodes or promoted_patterns), "nodes": promoted_nodes,
                "patterns": promoted_patterns,
                "groups": [{"pattern_id": pid, "config_hash": cfg, "successes": group["successes"],
                            "failures": group["failures"]} for pid, cfg, group in qualifying]}
    finally:
        conn.close()


def approve_pattern(pattern_id: str) -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        cur = conn.execute("UPDATE workflows SET trust = 'USER_APPROVED', updated_at = ? WHERE pattern_id = ?",
                           (_now(), pattern_id))
        conn.commit()
        return {"ok": cur.rowcount > 0, "pattern_id": pattern_id, "trust": "USER_APPROVED"}
    finally:
        conn.close()


# ---------------------------------------------------------------- research + freshness

WEB_TRUST_PREFIX = "WEB_"


def add_claims(entity_id: str, claims: list[Mapping[str, Any]], source: str = "",
               trust: str = "WEB_UNVERIFIED") -> dict[str, Any]:
    """Store externally researched claims as low-trust data with provenance.

    Research is evidence, never instructions, and never auto-promotes to CONFIRMED.
    """
    entity_id = str(entity_id or "").strip()
    if not entity_id or not isinstance(claims, list):
        return {"stored": 0, "entity": entity_id}
    conn = _connect()
    stored = 0
    try:
        _init(conn)
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            key = str(claim.get("key") or "").strip()
            value = claim.get("value")
            if not key or value in (None, ""):
                continue
            url = str(claim.get("source_url") or claim.get("source") or source or "")
            confidence = float(claim.get("confidence", 0.3) or 0.3)
            provenance = f"web:{url}" if url else "web"
            conn.execute(
                "INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entity_id, key, str(value), confidence, provenance, _now()))
            conn.execute(
                "INSERT INTO evidence (claim, source_type, source, entity, version, result, confidence, trust, retrieved_at) "
                "VALUES (?, 'web', ?, ?, '', ?, ?, ?, ?)",
                (f"{entity_id}: {key}={value}", url, entity_id, str(claim.get("notes") or ""),
                 confidence, trust, _now()))
            stored += 1
        conn.commit()
    finally:
        conn.close()
    return {"stored": stored, "entity": entity_id, "trust": trust}


def _comfyui_version() -> str:
    try:
        import comfyui_version
        return str(getattr(comfyui_version, "__version__", "") or "")
    except Exception:
        return ""


def _manifests_digest() -> str:
    digest = hashlib.sha256()
    if os.path.isdir(MANIFEST_DIR):
        for name in sorted(os.listdir(MANIFEST_DIR)):
            if not name.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(MANIFEST_DIR, name), "rb") as handle:
                    digest.update(name.encode())
                    digest.update(handle.read())
            except OSError:
                continue
    return digest.hexdigest()[:16]


def _stored_versions(conn: sqlite3.Connection) -> dict[str, str]:
    row = conn.execute("SELECT value FROM meta WHERE key = 'source_versions'").fetchone()
    if not row or not row[0]:
        return {}
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def record_versions(versions: Mapping[str, str]) -> dict[str, Any]:
    """Track source versions; when one changes, drop stale web-derived knowledge so it is re-researched."""
    conn = _connect()
    try:
        _init(conn)
        previous = _stored_versions(conn)
        changed = [key for key, value in versions.items() if previous.get(key) not in (None, str(value))]
        invalidated = 0
        if changed:
            invalidated += conn.execute("DELETE FROM properties WHERE source LIKE 'web%'").rowcount or 0
            invalidated += conn.execute("DELETE FROM evidence WHERE trust LIKE 'WEB_%'").rowcount or 0
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('source_versions', ?)",
                     (json.dumps(dict(versions)),))
        conn.commit()
        return {"changed": changed, "invalidated": invalidated, "versions": dict(versions)}
    finally:
        conn.close()


def freshness() -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        versions = _stored_versions(conn)
        web_properties = conn.execute("SELECT COUNT(*) FROM properties WHERE source LIKE 'web%'").fetchone()[0]
        web_evidence = conn.execute("SELECT COUNT(*) FROM evidence WHERE trust LIKE 'WEB_%'").fetchone()[0]
        return {"versions": versions, "web_properties": web_properties, "web_evidence": web_evidence}
    finally:
        conn.close()


# ---------------------------------------------------------------- reads

def _entity_property_rows(conn: sqlite3.Connection, entity_id: str) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for key, value, confidence, source in conn.execute(
            "SELECT key, value, confidence, source FROM properties WHERE entity_id = ?", (entity_id,)):
        rows.setdefault(key, []).append({"value": value, "confidence": float(confidence or 0.0), "source": source or ""})
    return rows


def _entity_properties(conn: sqlite3.Connection, entity_id: str) -> dict[str, list[str]]:
    props: dict[str, list[str]] = {}
    for key, value in conn.execute("SELECT key, value FROM properties WHERE entity_id = ?", (entity_id,)):
        props.setdefault(key, []).append(value)
    return props


def _load_index(conn: sqlite3.Connection) -> dict[str, Any]:
    requirements: list[dict[str, Any]] = []
    for row in conn.execute("SELECT scope_kind, scope_id, task, predicate, state_match, state_mismatch, "
                            "confidence, provenance FROM requirements"):
        try:
            predicate = json.loads(row[3])
        except (TypeError, json.JSONDecodeError):
            continue
        requirements.append({"scope_kind": row[0], "scope_id": row[1], "task": row[2] or "",
                             "predicate": predicate, "state_match": row[4], "state_mismatch": row[5],
                             "confidence": row[6], "provenance": row[7]})
    edges: list[dict[str, Any]] = []
    for row in conn.execute("SELECT subject, relation, object, state, confidence, version, provenance FROM edges"):
        edges.append({"subject": row[0], "relation": row[1], "object": row[2], "state": row[3],
                      "confidence": row[4], "version": row[5], "provenance": row[6]})
    roles: dict[str, list[str]] = {}
    for entity_id, value in conn.execute(
            "SELECT p.entity_id, p.value FROM properties p JOIN entities e ON e.id = p.entity_id "
            "WHERE p.key = 'roles' AND e.kind = 'node'"):
        node_type = entity_id.split(":", 1)[1] if ":" in entity_id else entity_id
        roles.setdefault(node_type, []).append(value)
    generic_nodes: set[str] = set()
    for (entity_id,) in conn.execute(
            "SELECT DISTINCT entity_id FROM properties WHERE key = 'generic_operation' "
            "AND lower(value) IN ('true', '1', 'yes')"):
        generic_nodes.add(entity_id.split(":", 1)[1] if str(entity_id).startswith("node:") else str(entity_id))
    packs: dict[str, str] = {}
    for entity_id, value in conn.execute(
            "SELECT p.entity_id, p.value FROM properties p JOIN entities e ON e.id = p.entity_id "
            "WHERE p.key = 'pack' AND e.kind = 'node'"):
        node_type = entity_id.split(":", 1)[1] if ":" in entity_id else entity_id
        if value:
            packs[node_type] = value
    return {"requirements": requirements, "edges": edges, "roles": roles,
            "generic_nodes": generic_nodes, "packs": packs}


def _match(values: list[str], predicate: Mapping[str, Any]) -> bool:
    op = str(predicate.get("op") or "in")
    expected = predicate.get("value")
    expected = expected if isinstance(expected, list) else [expected]
    vals = [str(item).lower() for item in values]
    exps = [str(item).lower() for item in expected if item is not None]
    if op == "exists":
        return bool(values)
    if op in ("eq", "in"):
        return bool(set(vals) & set(exps))
    if op == "contains":
        return any(needle in value for value in vals for needle in exps)
    if op == "matches":
        for value in vals:
            for pattern in exps:
                try:
                    if re.search(pattern, value):
                        return True
                except re.error:
                    continue
        return False
    return False


def requirement_advisory_reason(predicate: Mapping[str, Any], confidence: float, provenance: str) -> str:
    """Unverified restrictions can inform a response, but cannot exclude a node."""
    if predicate.get("op", "in") not in {"eq", "in", "contains", "matches", "exists"}:
        return "unsupported predicate operator"
    source = str(provenance or "").strip().lower()
    if not source or source.startswith(("llm", "web")):
        return "restriction has no verified source"
    if not confidence >= MIN_HARD_CONFIDENCE:
        return "restriction confidence is too low"
    return ""


def _evaluate(node_type: str, entity_id: str, top: Mapping[str, Any], task: str) -> dict[str, Any]:
    node_id = f"node:{node_type}"
    rows = top.get("prop_rows", {})
    confirmed = None
    observed = None
    incompatible = None
    for edge in top["edges"]:
        if edge["subject"] != entity_id or edge["object"] != node_id:
            continue
        if edge["state"] in (CONFIRMED_COMPATIBLE, CONFIRMED_INCOMPATIBLE):
            confirmed = confirmed or edge
        elif edge["relation"] == "incompatible_with":
            incompatible = incompatible or edge
        elif edge["state"] == INFERRED_COMPATIBLE:
            observed = observed or edge
    explicit = confirmed or incompatible
    if explicit:
        return {"node": node_type, "state": explicit["state"], "reason": f"explicit {explicit['relation']}",
                "matched": [], "missing": [], "requirements": 0,
                "confidence": explicit["confidence"], "provenance": explicit["provenance"] or "", "conflicts": []}
    if observed:
        return {"node": node_type, "state": INFERRED_COMPATIBLE, "reason": "observed usage",
                "matched": [], "missing": [], "requirements": 0,
                "confidence": observed["confidence"], "provenance": observed["provenance"] or "", "conflicts": []}

    roles = list(top["roles"].get(node_type, []))
    architecture = [item["value"] for item in rows.get("architecture_family", [])]
    applicable: list[dict[str, Any]] = []
    for req in top["requirements"]:
        if req["task"] and task and req["task"] != task:
            continue
        if req["scope_kind"] == "node" and req["scope_id"] == node_type:
            applicable.append(req)
        elif req["scope_kind"] == "role" and req["scope_id"] in roles:
            applicable.append(req)
        elif req["scope_kind"] == "architecture" and req["scope_id"] in architecture:
            applicable.append(req)
        elif req["scope_kind"] == "task" and req["scope_id"] == task and task:
            applicable.append(req)
    if not applicable:
        # Strict: absence of requirements is not permission. GENERIC only when documented as such.
        if node_type in top.get("generic_nodes", set()):
            return {"node": node_type, "state": GENERIC, "reason": "documented generic operation",
                    "matched": [], "missing": [], "requirements": 0, "confidence": 0.8,
                    "provenance": "generic", "conflicts": []}
        return {"node": node_type, "state": UNKNOWN, "reason": "no requirements declared for this scope",
                "matched": [], "missing": [], "requirements": 0, "confidence": 0.0,
                "provenance": "", "conflicts": []}

    matched: list[str] = []
    missing: list[str] = []
    mismatched: list[str] = []
    conflicts: list[dict[str, Any]] = []
    sources: set[str] = set()
    confidences: list[float] = []
    any_specific = False
    enforced = 0
    for req in applicable:
        predicate = req["predicate"]
        key = str(predicate.get("key") or "")
        advisory = requirement_advisory_reason(predicate, req["confidence"], req["provenance"])
        if advisory:
            conflicts.append({"key": key, "reason": "unverified_requirement", "detail": advisory,
                              "predicate": predicate, "confidence": req["confidence"],
                              "provenance": req["provenance"]})
            continue
        enforced += 1
        required = bool(predicate.get("required", True))
        generic = bool(predicate.get("generic", False))
        if not generic:
            any_specific = True
        claims = rows.get(key, [])
        trusted = [item for item in claims
                   if item["confidence"] >= MIN_HARD_CONFIDENCE
                   and not str(item["source"]).strip().lower().startswith(("web", "llm"))]
        if not trusted:
            if claims:
                conflicts.append({"key": key, "reason": "unverified", "values": claims})
            if required:
                missing.append(key)
            continue
        values = [item["value"] for item in trusted]
        if _match(values, predicate):
            matched.append(key)
            for item in trusted:
                sources.add(str(item["source"]))
                confidences.append(item["confidence"])
            nonmatching = [item for item in trusted if not _match([item["value"]], predicate)]
            if nonmatching:
                conflicts.append({"key": key, "reason": "partial_contradiction", "values": nonmatching})
        elif len(set(values)) > 1:
            conflicts.append({"key": key, "reason": "contradiction", "values": trusted})
            if required:
                missing.append(key)
        elif required:
            mismatched.append(key)
    summary = {
        "matched": matched, "missing": missing, "requirements": len(applicable),
        "confidence": round(min(confidences), 2) if confidences else 0.5,
        "provenance": ", ".join(sorted(sources)), "conflicts": conflicts,
    }
    if not enforced:
        generic = node_type in top.get("generic_nodes", set())
        return {"node": node_type, **summary, "state": GENERIC if generic else UNKNOWN,
                "reason": "documented generic operation; inferred restrictions are advisory" if generic
                          else "only unverified restrictions; compatibility is unknown",
                "confidence": 0.8 if generic else 0.0}
    if mismatched:
        return {"node": node_type, "state": INFERRED_INCOMPATIBLE,
                "reason": "property mismatch: " + ", ".join(sorted(set(mismatched))), **summary}
    if missing:
        return {"node": node_type, "state": UNKNOWN,
                "reason": "missing property: " + ", ".join(sorted(set(missing))), **summary}
    return {"node": node_type, "state": INFERRED_COMPATIBLE if any_specific else GENERIC,
            "reason": "all requirements satisfied", "missing": [], **summary}


def resolve(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith(("node:", "asset:")):
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute("SELECT id FROM entities WHERE id = ?", (raw,)).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()
    norm = _normalize(text)
    if not norm:
        return ""
    conn = _connect()
    try:
        _init(conn)
        row = conn.execute("SELECT id FROM entities WHERE lower(id) = ? OR lower(display) = ? LIMIT 1",
                           (norm, norm)).fetchone()
        if row:
            return row[0]
        row = conn.execute("SELECT entity_id FROM aliases WHERE alias_norm = ? ORDER BY confidence DESC, length(alias_norm) DESC LIMIT 1",
                           (norm,)).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "SELECT entity_id FROM aliases WHERE ? LIKE '%' || alias_norm || '%' OR alias_norm LIKE '%' || ? || '%' "
            "ORDER BY length(alias_norm) DESC, confidence DESC LIMIT 1", (norm, norm)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def evaluate(entity_id: str, node_type: str, task: str = "") -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        top = _load_index(conn)
        top["props"] = _entity_properties(conn, entity_id)
        top["prop_rows"] = _entity_property_rows(conn, entity_id)
        return _evaluate(node_type, entity_id, top, task)
    finally:
        conn.close()


def _candidate_nodes(nodes: list[str] | None) -> list[str]:
    if nodes is not None:
        return list(nodes)
    try:
        try:
            from . import node_catalog
        except ImportError:
            import node_catalog
        mapping, _ = node_catalog.registry()
        return list(mapping.keys())
    except Exception:
        return []


def _core_nodes() -> list[str]:
    """Built-in nodes (not under custom_nodes) as a generic default candidate set."""
    try:
        try:
            from . import node_catalog
        except ImportError:
            import node_catalog
        mapping, _ = node_catalog.registry()
    except Exception:
        return []
    result = []
    for node_type, cls in mapping.items():
        module = str(getattr(cls, "RELATIVE_PYTHON_MODULE", getattr(cls, "__module__", "")))
        if not module.startswith("custom_nodes."):
            result.append(str(node_type))
    return result


def _relevant_nodes(conn: sqlite3.Connection, entity_id: str, task: str = "") -> list[str]:
    """Bounded, ordered candidate set: known patterns, role/requirement nodes, then core nodes."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(name: Any) -> None:
        text = str(name or "")
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)

    for (nodes_json,) in conn.execute(
            "SELECT nodes FROM workflows WHERE (model_entity = ? OR model_entity = '') AND (task = ? OR task = '')",
            (entity_id, task)):
        try:
            for item in json.loads(nodes_json or "[]"):
                add(item)
        except (TypeError, json.JSONDecodeError):
            continue
    for (value,) in conn.execute(
            "SELECT DISTINCT e.id FROM properties p JOIN entities e ON e.id = p.entity_id "
            "WHERE p.key = 'roles' AND e.kind = 'node'"):
        add(value.split(":", 1)[1] if str(value).startswith("node:") else value)
    for (scope_id,) in conn.execute("SELECT DISTINCT scope_id FROM requirements WHERE scope_kind = 'node'"):
        add(scope_id)
    for node_type in _core_nodes():
        add(node_type)
    return ordered


def compatible_nodes(entity_id: str, task: str = "", nodes: list[str] | None = None) -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        top = _load_index(conn)
        top["props"] = _entity_properties(conn, entity_id)
        top["prop_rows"] = _entity_property_rows(conn, entity_id)
        allowed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        for node_type in _candidate_nodes(nodes):
            result = _evaluate(node_type, entity_id, top, task)
            entry = {"node": node_type, "state": result["state"], "reason": result["reason"],
                     "roles": top["roles"].get(node_type, []),
                     "pack": top.get("packs", {}).get(node_type, ""),
                     "confidence": result.get("confidence", 0.5),
                     "provenance": result.get("provenance", ""),
                     "conflicts": result.get("conflicts", []),
                     "warn": result["state"] in WARN_STATES or any(
                         conflict.get("reason") == "unverified_requirement" for conflict in result.get("conflicts", []))}
            if result["state"] in ALLOWED_STATES:
                allowed.append(entry)
            elif result["state"] in BLOCKED_STATES:
                blocked.append(entry)
            else:
                unknown.append(entry)
        return {"entity": entity_id, "task": task, "allowed": allowed, "blocked": blocked,
                "unknown": unknown,
                "counts": {"allowed": len(allowed), "blocked": len(blocked), "unknown": len(unknown)}}
    finally:
        conn.close()


def assets_for(entity_id: str = "", roles: list[str] | None = None) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        _init(conn)
        clauses: list[str] = []
        params: list[Any] = []
        if entity_id:
            clauses.append("(entity_id = ? OR entity_id IS NULL)")
            params.append(entity_id)
        if roles:
            marks = ",".join("?" for _ in roles)
            clauses.append(f"role IN ({marks})")
            params.extend(roles)
        sql = "SELECT id, filename, path, role, size, entity_id FROM assets"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY filename LIMIT 200"
        return [{"id": row[0], "filename": row[1], "path": row[2], "role": row[3],
                 "size": row[4], "entity": row[5]} for row in conn.execute(sql, params)]
    finally:
        conn.close()


def get_build_context(model_text: str, task: str = "", nodes: list[str] | None = None) -> dict[str, Any]:
    entity_id = resolve(model_text)
    if not entity_id:
        return {"resolved": False, "query": model_text, "task": task,
                "reason": "No entity resolved. Treat as UNKNOWN and report, do not guess.",
                "candidates": [row[0] for row in _suggest(model_text)]}
    conn = _connect()
    try:
        _init(conn)
        row = conn.execute("SELECT kind, display, trust, confidence, provenance FROM entities WHERE id = ?",
                           (entity_id,)).fetchone()
        props = _entity_properties(conn, entity_id)
        top = _load_index(conn)
        requirements = [req for req in top["requirements"]
                        if (req["scope_kind"] == "architecture" and req["scope_id"] in props.get("architecture_family", []))
                        or (req["scope_kind"] == "task" and req["task"] == task)]
        patterns = [{"pattern_id": r[0], "task": r[1], "nodes": json.loads(r[2] or "[]"), "trust": r[3]}
                    for r in conn.execute("SELECT pattern_id, task, nodes, trust FROM workflows "
                                          "WHERE (model_entity = ? OR model_entity = '') AND (task = ? OR task = '') "
                                          "ORDER BY CASE trust WHEN 'USER_APPROVED' THEN 0 WHEN 'TESTED' THEN 1 "
                                          "WHEN 'OBSERVED' THEN 2 WHEN 'DOCUMENTED' THEN 3 ELSE 4 END LIMIT 3",
                                          (entity_id, task))]
        evidence = [{"claim": r[0], "source_type": r[1], "source": r[2], "result": r[3], "trust": r[4]}
                    for r in conn.execute("SELECT claim, source_type, source, result, trust FROM evidence "
                                          "WHERE entity = ? OR entity IS NULL OR entity = '' "
                                          "ORDER BY retrieved_at DESC LIMIT 10", (entity_id,))]
        if nodes is None:
            nodes = _relevant_nodes(conn, entity_id, task)
    finally:
        conn.close()
    compatibility = compatible_nodes(entity_id, task, nodes)
    roles = sorted({role for entry in compatibility["allowed"] for role in entry.get("roles", [])})
    research_targets = sorted({entry.get("pack") for entry in compatibility["unknown"] if entry.get("pack")})
    return {
        "resolved": True,
        "query": model_text,
        "target_model": {"id": entity_id, "kind": row[0], "display": row[1],
                         "trust": row[2], "confidence": row[3]},
        "properties": props,
        "requirements": [{"scope": req["scope_kind"], "id": req["scope_id"], "predicate": req["predicate"]}
                         for req in requirements],
        "compatible_nodes": compatibility["allowed"] + compatibility["unknown"],
        "unknown_nodes": compatibility["unknown"],
        "exclusions": compatibility["blocked"],
        "roles": roles,
        "installed_assets": assets_for(entity_id, roles),
        "known_good_patterns": patterns,
        "evidence": evidence,
        "research_targets": research_targets[:5],
        "counts": compatibility["counts"],
        "policy": {"usable_states": sorted(ALLOWED_STATES), "warn_states": sorted(WARN_STATES),
                   "blocked_states": sorted(BLOCKED_STATES),
                   "unknown": "usable but unverified (may be wrong); prefer verified nodes"},
    }


def _suggest(text: str) -> list[tuple[str, str]]:
    tokens = [token for token in _normalize(text).split() if len(token) >= 2]
    conn = _connect()
    try:
        _init(conn)
        rows = conn.execute("SELECT id, display FROM entities WHERE kind != 'node' ORDER BY id").fetchall()
    finally:
        conn.close()
    if not tokens:
        return list(rows[:20])
    scored = []
    for entity_id, display in rows:
        haystack = f"{_normalize(entity_id)} {_normalize(display)}"
        score = sum(1 for token in tokens if token in haystack)
        if score:
            scored.append((score, entity_id, display))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(entity_id, display) for _score, entity_id, display in scored[:20]]


def stats() -> dict[str, Any]:
    conn = _connect()
    try:
        _init(conn)
        result: dict[str, Any] = {}
        for table in ("entities", "properties", "aliases", "requirements", "edges", "assets",
                      "evidence", "workflows", "experiences"):
            result[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        result["entities_by_kind"] = {row[0]: row[1] for row in
                                      conn.execute("SELECT kind, COUNT(*) FROM entities GROUP BY kind")}
        return result
    finally:
        conn.close()


def status() -> dict[str, Any]:
    with _lock:
        snapshot = dict(_state)
    try:
        snapshot.update(stats())
        snapshot.update(freshness())
    except Exception:
        pass
    return snapshot


# ---------------------------------------------------------------- ingestion

def _clear_source(conn: sqlite3.Connection, source: str) -> None:
    like = f"{source}%"
    conn.execute("DELETE FROM requirements WHERE provenance LIKE ?", (like,))
    conn.execute("DELETE FROM edges WHERE provenance LIKE ?", (like,))
    conn.execute("DELETE FROM aliases WHERE source LIKE ?", (like,))
    conn.execute("DELETE FROM properties WHERE source LIKE ?", (like,))
    conn.execute("DELETE FROM entities WHERE provenance LIKE ?", (like,))
    conn.execute("DELETE FROM workflows WHERE provenance LIKE ?", (like,))


def load_manifests(directory: str = "") -> list[dict[str, Any]]:
    directory = directory or MANIFEST_DIR
    manifests: list[dict[str, Any]] = []
    if not os.path.isdir(directory):
        return manifests
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("entities"), list):
                data["_name"] = name
                manifests.append(data)
        except (OSError, json.JSONDecodeError) as exc:
            _debug_log("knowledge", "manifest.error", level="warning", manifest=name, error=str(exc))
    return manifests


def _apply_manifest(conn: sqlite3.Connection, manifest: Mapping[str, Any]) -> int:
    source = f"manifest:{manifest.get('_name', 'unknown')}"
    applied = 0
    for entity in manifest.get("entities", []) or []:
        entity_id = str(entity.get("id") or "").strip()
        if not entity_id:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO entities (id, kind, display, knowledge_type, trust, confidence, provenance, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_id, str(entity.get("kind") or "model"), str(entity.get("display") or entity_id),
             str(entity.get("knowledge_type") or "fact"), str(entity.get("trust") or "DOCUMENTED"),
             float(entity.get("confidence", 0.7)), source, _now()),
        )
        for alias in entity.get("aliases", []) or []:
            norm = _normalize(alias)
            if norm:
                conn.execute("INSERT OR REPLACE INTO aliases (alias, alias_norm, entity_id, kind, source, confidence) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (str(alias), norm, entity_id, str(entity.get("kind") or "model"), source,
                              float(entity.get("confidence", 0.7))))
        for key, value in (entity.get("properties") or {}).items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                conn.execute("INSERT OR REPLACE INTO properties (entity_id, key, value, confidence, source, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (entity_id, str(key), str(item), float(entity.get("confidence", 0.7)), source, _now()))
        applied += 1
    for requirement in manifest.get("requirements", []) or []:
        try:
            conn.execute(
                "INSERT INTO requirements (scope_kind, scope_id, task, predicate, state_match, state_mismatch, "
                "confidence, provenance, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(requirement.get("scope_kind") or "node"), str(requirement.get("scope_id") or ""),
                 str(requirement.get("task") or ""), json.dumps(requirement.get("predicate") or {}),
                 str(requirement.get("state_match") or INFERRED_COMPATIBLE),
                 str(requirement.get("state_mismatch") or INFERRED_INCOMPATIBLE),
                 float(requirement.get("confidence", 0.6)), source, _now()),
            )
            applied += 1
        except (ValueError, TypeError):
            continue
    for edge in manifest.get("edges", []) or []:
        try:
            conn.execute("INSERT OR REPLACE INTO edges (subject, relation, object, state, confidence, version, provenance, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (str(edge.get("subject")), str(edge.get("relation") or "compatible_with"),
                          str(edge.get("object")), str(edge.get("state") or CONFIRMED_COMPATIBLE),
                          float(edge.get("confidence", 0.9)), str(edge.get("version") or ""), source, _now()))
            applied += 1
        except (TypeError, ValueError):
            continue
    for workflow in manifest.get("workflows", []) or []:
        conn.execute("INSERT OR REPLACE INTO workflows (pattern_id, model_entity, task, nodes, trust, provenance, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (str(workflow.get("pattern_id")), str(workflow.get("model_entity") or ""),
                      str(workflow.get("task") or ""), json.dumps(workflow.get("nodes") or []),
                      str(workflow.get("trust") or "DOCUMENTED"), source, _now()))
        applied += 1
    return applied


def _ingest_nodes() -> int:
    try:
        try:
            from . import node_catalog
        except ImportError:
            import node_catalog
        mapping, names = node_catalog.registry()
    except Exception:
        return 0
    conn = _connect()
    applied = 0
    try:
        _init(conn)
        for node_type, cls in mapping.items():
            entity_id = f"node:{node_type}"
            display = names.get(node_type, node_type)
            conn.execute("INSERT OR IGNORE INTO entities (id, kind, display, knowledge_type, trust, confidence, provenance, updated_at) "
                         "VALUES (?, 'node', ?, 'fact', 'DOCUMENTED', 0.9, 'runtime:nodes', ?)",
                         (entity_id, str(display), _now()))
            conn.execute("INSERT OR REPLACE INTO aliases (alias, alias_norm, entity_id, kind, source, confidence) "
                         "VALUES (?, ?, ?, 'node', 'runtime:nodes', 0.8)",
                         (str(display), _normalize(display), entity_id))
            for alias in (getattr(cls, "SEARCH_ALIASES", None) or []):
                norm = _normalize(alias)
                if norm:
                    conn.execute("INSERT OR REPLACE INTO aliases (alias, alias_norm, entity_id, kind, source, confidence) "
                                 "VALUES (?, ?, ?, 'node', 'runtime:nodes', 0.6)",
                                 (str(alias), norm, entity_id))
            applied += 1
        conn.commit()
    finally:
        conn.close()
    return applied


def scan_assets() -> int:
    try:
        import folder_paths
    except ImportError:
        return 0
    conn = _connect()
    applied = 0
    try:
        _init(conn)
        conn.execute("DELETE FROM assets")
        for folder, role in ROLE_BY_FOLDER.items():
            try:
                paths = folder_paths.get_folder_paths(folder)
            except Exception:
                continue
            for base in paths:
                if not os.path.isdir(base):
                    continue
                for entry in os.scandir(base):
                    if not entry.is_file():
                        continue
                    try:
                        info = entry.stat()
                    except OSError:
                        continue
                    asset_id = f"asset:{folder}:{entry.name}"
                    conn.execute(
                        "INSERT OR REPLACE INTO assets (id, filename, path, role, size, mtime, sha256, entity_id, source, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'runtime:assets', ?)",
                        (asset_id, entry.name, entry.path, role, int(info.st_size), float(info.st_mtime),
                         _guess_asset_entity(conn, entry.name), _now()),
                    )
                    applied += 1
        conn.commit()
    finally:
        conn.close()
    return applied


def _guess_asset_entity(conn: sqlite3.Connection, filename: str) -> str:
    norm = _normalize(filename)
    if not norm:
        return ""
    row = conn.execute(
        "SELECT a.entity_id FROM aliases a JOIN entities e ON e.id = a.entity_id "
        "WHERE e.kind = 'model' AND a.alias_norm != '' AND instr(?, a.alias_norm) > 0 "
        "ORDER BY length(a.alias_norm) DESC LIMIT 1", (norm,)).fetchone()
    return row[0] if row else ""


def _purge_legacy_web(conn: sqlite3.Connection) -> int:
    """One-time cleanup of stray low-trust web claims from earlier ad-hoc research."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'web_cleanup_v1'").fetchone()
    if row and row[0]:
        return 0
    removed = conn.execute("DELETE FROM properties WHERE source LIKE 'web%'").rowcount or 0
    conn.execute("DELETE FROM evidence WHERE trust LIKE 'WEB_%'")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('web_cleanup_v1', '1')")
    conn.commit()
    return removed


def index_knowledge() -> int:
    """Rebuild curated + runtime-derived knowledge. Curated rows are re-applied idempotently."""
    conn = _connect()
    try:
        _init(conn)
        _clear_source(conn, "manifest:")
        _clear_source(conn, "runtime:nodes")
        conn.commit()
        _purge_legacy_web(conn)
        applied = 0
        for manifest in load_manifests():
            applied += _apply_manifest(conn, manifest)
        conn.commit()
    finally:
        conn.close()
    nodes = _ingest_nodes()
    assets = scan_assets()
    try:
        record_versions({"comfyui": _comfyui_version(), "manifests": _manifests_digest()})
    except Exception as exc:
        _debug_log("knowledge", "versions.error", level="warning", error=str(exc))
    # Manifests may declare node entities/roles; nodes ingestion must not wipe them.
    with _lock:
        _state["last_index"] = _now()
        _state["indexed"] = applied
        _state["error"] = ""
    _debug_log("knowledge", "index.done", manifest_rows=applied, nodes=nodes, assets=assets)
    return applied
