from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any

import folder_paths

GIT_URL_RE = re.compile(r"^(https?://|git://|ssh://|git@)")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
INVALID_URL_CHARS = ("\n", "\r", ";", "|", "&", "`", "$")
DEFAULT_TIMEOUT = 900


def _custom_nodes_dir() -> str:
    paths = folder_paths.folder_names_and_paths.get("custom_nodes", ([], set()))[0]
    if not paths:
        raise RuntimeError("Could not resolve the ComfyUI custom_nodes directory.")
    return os.path.realpath(paths[0])


def folder_name_from_url(url: str) -> str:
    name = url.rstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    return name.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if name in (".", "..") or not SAFE_NAME_RE.match(name):
        return ""
    return name


def git_install(url: str, name: str | None = None, run_pip: bool = True) -> dict[str, Any]:
    if not isinstance(url, str) or not GIT_URL_RE.match(url):
        return {"ok": False, "error": "Only git URLs (https, ssh, git) are allowed."}
    if any(char in url for char in INVALID_URL_CHARS):
        return {"ok": False, "error": "URL contains invalid characters."}

    folder = _safe_name(name or "") or _safe_name(folder_name_from_url(url))
    if not folder:
        return {"ok": False, "error": "Could not determine a safe folder name for this repository."}

    try:
        root = _custom_nodes_dir()
        destination = os.path.realpath(os.path.join(root, folder))
        if os.path.commonpath([root, destination]) != root or destination == root:
            return {"ok": False, "error": "Target path escapes the custom_nodes directory."}
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}

    if os.path.exists(destination):
        return {"ok": False, "error": f"A folder named '{folder}' already exists in custom_nodes."}

    clone = _run(["git", "clone", "--depth", "1", url, destination])
    result: dict[str, Any] = {
        "ok": clone["returncode"] == 0,
        "path": destination,
        "stdout": clone["stdout"],
        "stderr": clone["stderr"],
        "pip_output": "",
        "restart_required": True,
    }
    if not result["ok"]:
        result["error"] = clone["stderr"].strip() or "git clone failed."
        return result

    requirements = os.path.join(destination, "requirements.txt")
    if run_pip and os.path.isfile(requirements):
        pip = _run([sys.executable, "-m", "pip", "install", "-r", requirements])
        result["pip_output"] = (pip["stdout"] + pip["stderr"]).strip()
        result["pip_ok"] = pip["returncode"] == 0
    return result


def _run(command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
            shell=False,
        )
    except FileNotFoundError:
        return {"returncode": 1, "stdout": "", "stderr": "git executable not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"returncode": 1, "stdout": "", "stderr": "Command timed out."}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }
