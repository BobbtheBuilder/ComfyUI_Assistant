from __future__ import annotations

import re
import threading
from collections import deque
from typing import Any

MAX_LINES = 500
BUFFER_SIZE = 2000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ERROR_RE = re.compile(r"\b(ERROR|WARNING|CRITICAL|Traceback|Exception|Error|Warning)\b")

_lock = threading.RLock()
_buffer: deque[dict[str, Any]] = deque(maxlen=BUFFER_SIZE)
_registered = False


def _capture(entries: Any) -> None:
    if not isinstance(entries, (list, tuple)):
        return
    with _lock:
        for entry in entries:
            if isinstance(entry, dict):
                _buffer.append({"t": str(entry.get("t", "")), "m": str(entry.get("m", ""))})
            else:
                _buffer.append({"t": "", "m": str(entry)})


def _register() -> None:
    global _registered
    if _registered:
        return
    try:
        import app.logger as comfy_logger

        comfy_logger.on_flush(_capture)
        _registered = True
    except Exception:
        _registered = False


_register()


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _scrub(text: str) -> str:
    try:
        try:
            from . import debug
        except ImportError:
            import debug

        return debug.scrub_text(text)
    except Exception:
        return text


def recent(lines: int = MAX_LINES, level: str | None = None, entries: Any = None) -> dict[str, Any]:
    count = max(1, min(int(lines or MAX_LINES), MAX_LINES))
    if entries is not None:
        source = list(entries)
    else:
        with _lock:
            source = list(_buffer)
        if not source:
            try:
                import app.logger as comfy_logger

                source = list(comfy_logger.get_logs() or [])
            except Exception:
                source = []

    wanted = (level or "").strip().lower()
    collected: list[dict[str, str]] = []
    for entry in source:
        if isinstance(entry, dict):
            message = str(entry.get("m", ""))
            timestamp = str(entry.get("t", ""))
        else:
            message = str(entry)
            timestamp = ""
        message = _strip_ansi(message).rstrip("\n")
        if not message.strip():
            continue
        if wanted in ("error", "warning") and not _ERROR_RE.search(message):
            continue
        collected.append({"t": timestamp, "m": _scrub(message)})

    if len(collected) > count:
        collected = collected[-count:]
    return {"lines": collected, "total": len(collected), "available": True}
