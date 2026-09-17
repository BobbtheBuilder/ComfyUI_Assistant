from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(NODE_DIR, "debug.log")
SCHEMA_VERSION = 1

_lock = threading.RLock()
_events: deque[dict[str, Any]] | None = None


def _limit(name: str, default: int = 0) -> int:
    try:
        try:
            from .config_store import CONFIG_STORE
        except ImportError:
            from config_store import CONFIG_STORE
        return CONFIG_STORE.get_limit(name, default)
    except Exception:
        return default


def _buffer() -> deque[dict[str, Any]]:
    global _events
    if _events is None:
        size = _limit("debug_max_events", 100000)
        _events = deque() if size <= 0 else deque(maxlen=size)
    return _events


def _scrub_url(match: re.Match[str]) -> str:
    return re.sub(r"://[^/@\s]+@", "://", match.group(0))


_REDACTIONS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"https?://[^\s\"'<>]+"), _scrub_url),
    (re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+"), "<image-data>"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"), r"\1<redacted>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd|x-api-key)\b\s*[:=]\s*[\"']?([^\s\"',;}]+)"), r"\1=<redacted>"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "<redacted-key>"),
    (re.compile(r"\bBSA[A-Za-z0-9]{6,}"), "<redacted-key>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "<redacted-key>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "<redacted-key>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\s\"'<>]+", re.IGNORECASE), "<path>"),
    (re.compile(r"(?:/home|/Users)/[^\s\"'<>]+"), "<path>"),
    (re.compile(r"[A-Za-z]:\\[^\s\"'<>]+"), "<path>"),
]


def scrub_text(value: Any) -> str:
    text = str(value if value is not None else "")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    limit = _limit("debug_max_field_chars", 0)
    return text[:limit] if limit and limit > 0 else text


def scrub(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, str):
        return scrub_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {scrub_text(key): scrub(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [scrub(item, depth + 1) for item in value]
    return scrub_text(value)


def _enabled() -> bool:
    try:
        try:
            from .config_store import CONFIG_STORE
        except ImportError:
            from config_store import CONFIG_STORE
        return CONFIG_STORE.is_debug_enabled()
    except Exception:
        return False


def _iso(timestamp: float | None = None) -> str:
    moment = datetime.fromtimestamp(timestamp if timestamp is not None else time.time(), tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds")


def _write_file(line: str) -> None:
    try:
        size_limit = _limit("debug_max_file_bytes", 0)
        if size_limit and size_limit > 0 and os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > size_limit:
            backup = LOG_PATH + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(LOG_PATH, backup)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def log(category: str, event: str, level: str = "info", **data: Any) -> None:
    if not _enabled():
        return
    record = {
        "ts": _iso(),
        "level": level,
        "category": scrub_text(category),
        "event": scrub_text(event),
        "data": scrub(data),
    }
    with _lock:
        _buffer().append(record)
    _write_file(json.dumps(record, ensure_ascii=True))


def debug_log(*args: Any, **kwargs: Any) -> None:
    try:
        log(*args, **kwargs)
    except Exception:
        pass


def clear() -> None:
    with _lock:
        _buffer().clear()
    try:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
        backup = LOG_PATH + ".1"
        if os.path.exists(backup):
            os.remove(backup)
    except OSError:
        pass


def snapshot() -> dict[str, Any]:
    with _lock:
        events = list(_buffer())
    return {"enabled": _enabled(), "events": events}


def _app_version() -> str:
    try:
        try:
            from . import __version__ as version
        except ImportError:
            from __init__ import __version__ as version
        return str(version)
    except Exception:
        return "unknown"


def _comfyui_version() -> str:
    try:
        import comfyui_version

        return str(getattr(comfyui_version, "__version__", "") or "")
    except Exception:
        return ""


def _env() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "comfyui_version": _comfyui_version(),
    }


def _config_summary() -> dict[str, Any]:
    try:
        try:
            from .config_store import CONFIG_STORE
        except ImportError:
            from config_store import CONFIG_STORE
        config = CONFIG_STORE.resolved()
    except Exception as exc:
        return {"error": scrub_text(exc)}
    websearch = config.get("websearch", {}) or {}
    return {
        "provider": config.get("provider"),
        "model": scrub_text(config.get("model", "")),
        "has_custom_base_url": bool(config.get("base_url")),
        "temperature": config.get("temperature"),
        "max_tokens": config.get("max_tokens"),
        "use_native_tools": config.get("use_native_tools"),
        "thinking": config.get("thinking"),
        "websearch": {
            "provider": websearch.get("provider"),
            "has_key": bool(websearch.get("api_key")),
            "max_results": websearch.get("max_results"),
        },
        "flags": {
            "kb": config.get("kb", {}),
            "images": config.get("images", {}),
            "selection": config.get("selection", {}),
            "highlight": config.get("highlight", {}),
            "layout": config.get("layout", {}),
            "validate": config.get("validate", {}),
            "context": config.get("context", {}),
            "memory": config.get("memory", {}),
            "debug": config.get("debug", {}),
            "console": config.get("console", {}),
            "unload": config.get("unload", {}),
            "experience": config.get("experience", {}),
        },
    }


def _kb_summary() -> dict[str, Any]:
    try:
        try:
            from . import kb
        except ImportError:
            import kb
        status = kb.status()
        return {
            key: status.get(key)
            for key in ("building", "phase", "chunks", "files", "official_chunks", "last_build_iso", "official_fetched_iso", "error")
        }
    except Exception as exc:
        return {"error": scrub_text(exc)}


def _memory_summary() -> dict[str, Any]:
    try:
        try:
            from . import memory
        except ImportError:
            import memory
        return {"lessons": len(memory.list_lessons())}
    except Exception as exc:
        return {"error": scrub_text(exc)}


def report(client: Any = None) -> dict[str, Any]:
    with _lock:
        events = list(_buffer())
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(),
        "app": {"name": "ComfyUI Assistant", "version": _app_version(), "debug_enabled": _enabled()},
        "env": _env(),
        "config": _config_summary(),
        "kb": _kb_summary(),
        "memory": _memory_summary(),
        "events": events,
        "client": scrub(client) if client else None,
        "note": "No prompts, chat messages, API keys, file paths, or personal data are included in this report.",
    }


def report_text(bundle: dict[str, Any]) -> str:
    lines = [
        "ComfyUI Assistant debug report",
        f"generated: {bundle.get('generated_at')}  schema: {bundle.get('schema_version')}  debug: {(bundle.get('app') or {}).get('debug_enabled')}",
        f"app: {(bundle.get('app') or {}).get('name')} {(bundle.get('app') or {}).get('version')}",
        f"env: {json.dumps(bundle.get('env', {}))}",
        f"config: {json.dumps(bundle.get('config', {}))}",
        f"kb: {json.dumps(bundle.get('kb', {}))}",
        f"memory: {json.dumps(bundle.get('memory', {}))}",
        "",
        f"events ({len(bundle.get('events', []))}):",
    ]
    for event in bundle.get("events", []):
        lines.append(
            f"  {event.get('ts')} [{event.get('level')}] {event.get('category')}/{event.get('event')} "
            f"{json.dumps(event.get('data', {}))}"
        )
    if bundle.get("client"):
        lines.extend(["", f"client: {json.dumps(bundle.get('client'))}"])
    lines.extend(["", str(bundle.get("note", ""))])
    return "\n".join(lines)
