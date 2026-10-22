from __future__ import annotations

__version__ = "0.4.6"

from . import routes as _routes  # noqa: F401 - route registration is the import side effect.

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}

__all__ = ["__version__", "WEB_DIRECTORY", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
