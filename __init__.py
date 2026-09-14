from __future__ import annotations

__version__ = "0.2.0"

from . import kb
from . import routes as _routes  # noqa: F401 - route registration is the import side effect.
from .config_store import CONFIG_STORE

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}

_kb_config = CONFIG_STORE.resolved().get("kb", {})
if _kb_config.get("enabled", True):
    kb.start_background(
        auto_official=bool(_kb_config.get("auto_official", True)),
        refresh_days=int(_kb_config.get("refresh_days", 7)),
        delay=20,
    )

__all__ = ["__version__", "WEB_DIRECTORY", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
