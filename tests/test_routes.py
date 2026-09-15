"""Run route validation without importing/starting the ComfyUI server."""

import ast
import json
from pathlib import Path
from typing import Any, Mapping
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import web


class RouteValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "routes.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in functions:
            node.decorator_list = []
        self.config = Mock()
        self.scope = {"web": web, "json": json, "Any": Any, "Mapping": Mapping, "CONFIG_STORE": self.config}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), self.scope)
        self.body_handlers = [node.name for node in functions if node.name != "_json_object" and
                              any(isinstance(call, ast.Name) and call.id == "_json_object" for call in ast.walk(node))]

    async def test_non_object_bodies_rejected_by_all_json_routes(self):
        for name in self.body_handlers:
            for payload in (None, [], "text", 1):
                with self.subTest(route=name, payload=payload):
                    request = Mock(json=AsyncMock(return_value=payload))
                    with self.assertRaises(web.HTTPBadRequest) as error:
                        await self.scope[name](request)
                    self.assertEqual(json.loads(error.exception.text)["error"], "JSON body must be an object.")
        self.config.set_history.assert_not_called()

    async def test_invalid_history_cannot_delete_saved_conversation(self):
        request = Mock(json=AsyncMock(return_value={"messages": None}))
        response = await self.scope["set_history"](request)
        self.assertEqual(response.status, 400)
        self.config.set_history.assert_not_called()

    async def test_chat_validates_collections_before_opening_stream(self):
        for payload in ({"messages": None}, {"messages": ["bad"]}, {"tools": {}}, {"tools": [None]}):
            with self.subTest(payload=payload):
                response = await self.scope["chat"](Mock(json=AsyncMock(return_value=payload)))
                self.assertEqual(response.status, 400)
        self.config.resolved.assert_not_called()
