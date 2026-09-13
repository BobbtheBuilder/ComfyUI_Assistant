from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any, Mapping

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(NODE_DIR, "chatbot_config.json")
HISTORY_PATH = os.path.join(NODE_DIR, "chatbot_history.json")

DEFAULT_SYSTEM_PROMPT = (
    "You are the ComfyUI Assistent, an assistant embedded in the user's ComfyUI graph editor. "
    "You can inspect and edit the active workflow, search the local documentation knowledge base, "
    "and search the web. "
    "Use the provided tools to read the workflow, inspect nodes, add or remove nodes, connect "
    "and disconnect links, set widget values, and move nodes. "
    "Use search_docs and get_node_docs to ground explanations in the installed packs' documentation "
    "and the official ComfyUI docs; call them before guessing what a node does. "
    "Only use node types that exist in the user's installation; call search_installed_nodes first "
    "when you are unsure. Prefer asking a short clarifying question before making large changes. "
    "When you recommend a custom node pack that is not installed, give the repository URL and "
    "offer to install it with install_custom_node. "
    "You can only see images that are attached to a message and reported as [N image(s) attached]. "
    "If the user asks about an image and none is attached, say plainly that you cannot see it and "
    "ask them to attach it; never invent or guess image contents. "
    "When the user refers to \"this\", \"these\", or \"the selected node(s)\", act on the nodes listed "
    "in the canvas selection. When you explain how a node or a part of the workflow works, call "
    "highlight_nodes with the ids you are referring to so the user can see them on the canvas. "
    "After adding or rewiring nodes, call validate_workflow to confirm all required inputs are "
    "connected and fix any that are not, then call layout_workflow to arrange them readably. "
    "When the user corrects a mistake or states a lasting preference, call remember_lesson with a "
    "short, general rule so it is not repeated in future sessions."
)

DEFAULT_BASE_URLS = {
    "lmstudio": "http://127.0.0.1:1234/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}

DEFAULTS: dict[str, Any] = {
    "provider": "lmstudio",
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": 0.7,
    "max_tokens": 2048,
    "use_native_tools": True,
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "websearch": {
        "provider": "tavily",
        "api_key": "",
        "max_results": 5,
    },
    "kb": {
        "enabled": True,
        "auto_official": True,
        "refresh_days": 7,
    },
    "images": {
        "always_output": False,
        "always_inputs": False,
        "auto_on_mention": True,
        "max_dimension": 1024,
    },
    "selection": {
        "enabled": True,
    },
    "highlight": {
        "enabled": True,
    },
    "layout": {
        "auto_after_build": True,
    },
    "validate": {
        "auto_fix": True,
        "max_passes": 1,
    },
    "context": {
        "window": 0,
        "auto_compact": True,
        "keep_last": 12,
    },
    "memory": {
        "enabled": True,
        "auto_detect": True,
        "inject_limit": 8,
    },
    "debug": {
        "enabled": False,
    },
}


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: str, payload: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp, path)


class ConfigStore:
    def __init__(self, path: str = CONFIG_PATH, history_path: str = HISTORY_PATH):
        self._path = path
        self._history_path = history_path
        self._lock = threading.RLock()
        self._config = self._load()

    def _load(self) -> dict[str, Any]:
        config = deepcopy(DEFAULTS)
        stored = _read_json(self._path)
        if isinstance(stored, dict):
            for key, default in DEFAULTS.items():
                if isinstance(default, dict):
                    if isinstance(stored.get(key), dict):
                        config[key].update({k: v for k, v in stored[key].items() if k in default})
                elif key in stored:
                    config[key] = stored[key]
        return config

    def snapshot(self, include_secrets: bool = False) -> dict[str, Any]:
        with self._lock:
            config = deepcopy(self._config)
        config["base_url"] = config.get("base_url") or DEFAULT_BASE_URLS.get(config["provider"], "")
        if not include_secrets:
            config["api_key_configured"] = bool(self._config.get("api_key"))
            config["api_key"] = ""
            websearch = config.get("websearch", {})
            websearch["api_key_configured"] = bool(self._config.get("websearch", {}).get("api_key"))
            websearch["api_key"] = ""
        return config

    def update(self, values: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise ValueError("Configuration body must be a JSON object.")
        with self._lock:
            for key, value in values.items():
                if isinstance(DEFAULTS.get(key), Mapping):
                    if not isinstance(value, Mapping):
                        continue
                    target = self._config.setdefault(key, {})
                    for sub_key, sub_value in value.items():
                        if sub_key not in DEFAULTS[key]:
                            continue
                        if sub_key == "api_key" and not sub_value:
                            continue
                        target[sub_key] = sub_value
                elif key == "api_key":
                    if value:
                        self._config["api_key"] = value
                elif key in DEFAULTS:
                    self._config[key] = value
            _write_json(self._path, self._config)
            return self.snapshot(include_secrets=False)

    def resolved(self) -> dict[str, Any]:
        with self._lock:
            config = deepcopy(self._config)
        config["base_url"] = config.get("base_url") or DEFAULT_BASE_URLS.get(config["provider"], "")
        return config

    def is_debug_enabled(self) -> bool:
        with self._lock:
            return bool(self._config.get("debug", {}).get("enabled"))

    def history(self, key: str = "__default__") -> list[Any]:
        stored = _read_json(self._history_path)
        if not isinstance(stored, dict):
            return []
        messages = stored.get(key)
        return messages if isinstance(messages, list) else []

    def set_history(self, key: str, messages: list[Any]) -> None:
        with self._lock:
            stored = _read_json(self._history_path)
            data = stored if isinstance(stored, dict) else {}
            if isinstance(messages, list) and messages:
                data[key] = messages
            else:
                data.pop(key, None)
            _write_json(self._history_path, data)


CONFIG_STORE = ConfigStore()
