"""Run route validation without importing/starting the ComfyUI server."""

import ast
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

NODE_DIR = Path(__file__).resolve().parents[1]


def _load_routes_module():
    package_name = "assistant_routes_under_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(NODE_DIR)]
    sys.modules[package_name] = package

    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(
        instance=types.SimpleNamespace(routes=web.RouteTableDef())
    )
    sys.modules["server"] = server

    spec = importlib.util.spec_from_file_location(f"{package_name}.routes", str(NODE_DIR / "routes.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RouteValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.routes_module = _load_routes_module()
        self.config = Mock()
        self.routes_module.CONFIG_STORE = self.config
        tree = ast.parse((NODE_DIR / "routes.py").read_text(encoding="utf-8"))
        self.body_handlers = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != "_json_object"
            and any(isinstance(call, ast.Name) and call.id == "_json_object" for call in ast.walk(node))
        ]

    def handler(self, name):
        return getattr(self.routes_module, name)

    async def test_non_object_bodies_rejected_by_all_json_routes(self):
        for name in self.body_handlers:
            for payload in (None, [], "text", 1):
                with self.subTest(route=name, payload=payload):
                    request = Mock(json=AsyncMock(return_value=payload))
                    with self.assertRaises(web.HTTPBadRequest) as error:
                        await self.handler(name)(request)
                    self.assertEqual(json.loads(error.exception.text)["error"], "JSON body must be an object.")
        self.config.set_history.assert_not_called()

    async def test_invalid_history_cannot_delete_saved_conversation(self):
        request = Mock(json=AsyncMock(return_value={"messages": None}))
        response = await self.handler("set_history")(request)
        self.assertEqual(response.status, 400)
        self.config.set_history.assert_not_called()

    async def test_chat_validates_collections_before_opening_stream(self):
        for payload in ({"messages": None}, {"messages": ["bad"]}, {"tools": {}}, {"tools": [None]}):
            with self.subTest(payload=payload):
                response = await self.handler("chat")(Mock(json=AsyncMock(return_value=payload)))
                self.assertEqual(response.status, 400)
        self.config.resolved.assert_not_called()

    async def test_memory_add_surfaces_merge_result(self):
        self.config.resolved.return_value = {}
        merged = {"id": 7, "updated": True, "merged": True, "similarity": 0.8}
        with patch.object(self.routes_module, "_lesson_vector", new=AsyncMock(return_value=None)), \
                patch.object(self.routes_module.memory, "add_lesson", return_value=merged), \
                patch.object(self.routes_module.memory, "list_lessons", return_value=[]):
            request = Mock(json=AsyncMock(return_value={"action": "add", "text": "a rule"}))
            response = await self.handler("memory_update")(request)
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.text)["result"]["merged"])
