"""Offline tests for providers.py request/stream parsing, using a fake local server."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

NODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NODE_DIR not in sys.path:
    sys.path.insert(0, NODE_DIR)

from aiohttp import web  # noqa: E402

import providers  # noqa: E402


class StreamParsingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = web.Application()
        self.app.router.add_post("/v1/chat/completions", self._openai_stream)
        self.app.router.add_post("/v1/messages", self._anthropic_stream)
        self.app.router.add_post("/v1/bad/chat/completions", self._server_error)
        self.app.router.add_post("/v1/stream-error/chat/completions", self._stream_error)
        self.app.router.add_post("/v1/reasoning/chat/completions", self._openai_reasoning_stream)
        self.app.router.add_post("/reasoning/v1/messages", self._anthropic_reasoning_stream)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.site = site
        port = site._server.sockets[0].getsockname()[1]
        self.root = f"http://127.0.0.1:{port}"
        self.base = f"{self.root}/v1"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def _stream_error(self, request):
        return web.Response(text='data: {"error": {"message": "model failed"}}\n\n', content_type="text/event-stream")

    async def test_openai_in_stream_error_is_not_silent_success(self):
        events = [event async for event in providers._openai_chat(self._config(f"{self.base}/stream-error"), [], [])]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("model failed", events[0]["error"])

    # --- handlers ---------------------------------------------------------- #

    async def _openai_stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        payloads = [
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "search_docs", "arguments": '{"query":'}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"sampler"}'}}
            ]}, "finish_reason": "tool_calls"}]},
        ]
        for payload in payloads:
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    async def _anthropic_stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        payloads = [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "t1", "name": "search_docs"}},
            {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"query": "x"}'}},
            {"type": "content_block_stop"},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        ]
        for payload in payloads:
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
        return response

    async def _openai_reasoning_stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        payloads = [
            {"choices": [{"delta": {"reasoning_content": "let me "}}]},
            {"choices": [{"delta": {"reasoning_content": "think"}}]},
            {"choices": [{"delta": {"content": "Answer"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        for payload in payloads:
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    async def _anthropic_reasoning_stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        payloads = [
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "step "}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "one"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Done"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        ]
        for payload in payloads:
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
        return response

    async def _server_error(self, request):
        return web.Response(status=500, text="boom")

    # --- tests ------------------------------------------------------------- #

    def _config(self, base_url, **overrides):
        config = {
            "provider": "openai",
            "base_url": base_url,
            "api_key": "test-key",
            "model": "test-model",
            "temperature": 0.0,
            "max_tokens": 64,
            "use_native_tools": True,
        }
        config.update(overrides)
        return config

    async def test_openai_streams_text_and_accumulates_tool_calls(self):
        events = [
            event
            async for event in providers._openai_chat(
                self._config(self.base), [{"role": "user", "content": "hi"}], []
            )
        ]
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"), "Hello")
        calls = [e for e in events if e["type"] == "tool_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "search_docs")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"query": "sampler"})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["finish_reason"], "tool_calls")

    async def test_openai_http_error_yields_error_event(self):
        events = [
            event
            async for event in providers._openai_chat(
                self._config(f"{self.base}/bad"), [{"role": "user", "content": "hi"}], []
            )
        ]
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("500", events[0]["error"])

    async def test_anthropic_streams_text_and_tool_use(self):
        events = [
            event
            async for event in providers._anthropic_chat(
                self._config(self.root, provider="anthropic"), [{"role": "user", "content": "hi"}], []
            )
        ]
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"), "Hi")
        calls = [e for e in events if e["type"] == "tool_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "search_docs")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"query": "x"})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["finish_reason"], "tool_use")

    async def test_openai_reasoning_is_streamed_before_text(self):
        events = [
            event
            async for event in providers._openai_chat(
                self._config(f"{self.base}/reasoning"), [{"role": "user", "content": "hi"}], []
            )
        ]
        self.assertEqual([event["type"] for event in events][:2], ["reasoning", "reasoning"])
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "reasoning"), "let me think")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"), "Answer")
        self.assertEqual(events[-1]["type"], "done")

    async def test_reasoning_events_are_omitted_when_disabled(self):
        events = [
            event
            async for event in providers._openai_chat(
                self._config(f"{self.base}/reasoning", thinking={"enabled": False}),
                [{"role": "user", "content": "hi"}],
                [],
            )
        ]
        self.assertFalse(any(event["type"] == "reasoning" for event in events))
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"), "Answer")

    async def test_anthropic_thinking_delta_is_streamed(self):
        events = [
            event
            async for event in providers._anthropic_chat(
                self._config(f"{self.root}/reasoning", provider="anthropic"),
                [{"role": "user", "content": "hi"}],
                [],
            )
        ]
        self.assertEqual([event["type"] for event in events][:2], ["reasoning", "reasoning"])
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "reasoning"), "step one")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"), "Done")

    async def test_chat_events_reports_missing_model(self):
        events = [event async for event in providers.chat_events({"provider": "openai", "model": ""}, [], [])]
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("No model", events[0]["error"])


class ConversionHelpersTest(unittest.TestCase):
    def test_with_system_protocol_inserts_into_existing_system(self):
        messages = [{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "search_docs", "description": "docs"}}]
        prepared = providers._with_system_protocol(messages, tools)
        self.assertEqual(len(prepared), 2)
        self.assertTrue(prepared[0]["content"].startswith("base"))
        self.assertIn("search_docs", prepared[0]["content"])

    def test_with_system_protocol_prepends_when_missing(self):
        prepared = providers._with_system_protocol([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "x"}}])
        self.assertEqual(prepared[0]["role"], "system")
        self.assertEqual(prepared[1]["role"], "user")

    def test_anthropic_tools_conversion(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_docs",
                    "description": "docs",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ]
        converted = providers._anthropic_tools(tools)
        self.assertEqual(converted[0]["name"], "search_docs")
        self.assertIn("input_schema", converted[0])

    def test_anthropic_messages_handles_tool_calls(self):
        messages = [
            {"role": "system", "content": "base"},
            {
                "role": "assistant",
                "content": "working",
                "tool_calls": [{"id": "t1", "function": {"name": "search_docs", "arguments": '{"query": "x"}'}}],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
        ]
        system, converted = providers._anthropic_messages(messages)
        self.assertEqual(system, "base")
        self.assertEqual(converted[0]["role"], "assistant")
        self.assertEqual(converted[0]["content"][1]["type"], "tool_use")
        self.assertEqual(converted[0]["content"][1]["input"], {"query": "x"})
        self.assertEqual(converted[1]["role"], "user")
        self.assertEqual(converted[1]["content"][0]["type"], "tool_result")

    def test_anthropic_content_converts_image_url(self):
        content = [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        blocks = providers._anthropic_content(content)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")

    def test_anthropic_messages_merges_consecutive_user_turns(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
        _system, converted = providers._anthropic_messages(messages)
        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0]["role"], "user")
        self.assertEqual([block["text"] for block in converted[0]["content"]], ["first", "second"])

    def test_anthropic_messages_merges_injected_user_after_tool_result(self):
        messages = [
            {"role": "assistant", "content": "working", "tool_calls": [
                {"id": "t1", "function": {"name": "search_docs", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
            {"role": "user", "content": "stop, use the other node"},
        ]
        _system, converted = providers._anthropic_messages(messages)
        self.assertEqual([message["role"] for message in converted], ["assistant", "user"])
        self.assertEqual(converted[1]["content"][-1]["text"], "stop, use the other node")

    def test_base_url_trims_trailing_slash(self):
        self.assertEqual(providers._base_url({"base_url": "http://x/v1/"}), "http://x/v1")
        self.assertEqual(providers._api_root({"base_url": "http://x/v1"}), "http://x")


class EmbeddingLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        providers._window_cache.clear()

    async def test_explicit_override_wins(self):
        tokens, chars, source = await providers.embedding_limits(
            {"limits": {"embed_max_tokens": 1000, "embed_chars_per_token": 4}}, "m"
        )
        self.assertEqual((tokens, source), (1000, "manual"))
        self.assertEqual(chars, int(1000 * 4 * 0.9))

    async def test_detected_model_window_is_used(self):
        config = {"limits": {}, "provider": "lmstudio", "base_url": "http://x/v1", "model": "embed"}
        with patch.object(providers, "context_window", new=AsyncMock(return_value=2048)):
            tokens, _chars, source = await providers.embedding_limits(config, "embed")
        self.assertEqual((tokens, source), (2048, "model"))

    async def test_fallback_when_provider_reports_no_window(self):
        config = {"limits": {}, "provider": "openai", "base_url": "http://x/v1", "model": "embed"}
        with patch.object(providers, "context_window", new=AsyncMock(return_value=None)):
            tokens, _chars, source = await providers.embedding_limits(config, "embed")
        self.assertEqual((tokens, source), (512, "fallback"))


class ResolvedMaxTokensTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        providers._window_cache.clear()

    async def test_explicit_setting_wins_without_probing(self):
        probe = AsyncMock(return_value=100000)
        with patch.object(providers, "context_window", new=probe):
            value = await providers.resolved_max_tokens({"max_tokens": 512, "provider": "openai"})
        self.assertEqual(value, 512)
        probe.assert_not_awaited()

    async def test_configured_context_window_halves_without_probing(self):
        probe = AsyncMock(return_value=None)
        config = {"max_tokens": 0, "context": {"window": 40000}}
        with patch.object(providers, "context_window", new=probe):
            value = await providers.resolved_max_tokens(config)
        self.assertEqual(value, 20000)
        probe.assert_not_awaited()

    async def test_detected_window_halves(self):
        probe = AsyncMock(return_value=32768)
        config = {"max_tokens": 0, "provider": "lmstudio", "base_url": "http://x/v1", "model": "m"}
        with patch.object(providers, "context_window", new=probe):
            value = await providers.resolved_max_tokens(config)
        self.assertEqual(value, 16384)
        probe.assert_awaited_once()

    async def test_unknown_window_returns_none(self):
        config = {"max_tokens": 0, "provider": "openai", "base_url": "http://x/v1", "model": "m"}
        with patch.object(providers, "context_window", new=AsyncMock(return_value=None)):
            value = await providers.resolved_max_tokens(config)
        self.assertIsNone(value)

    async def test_window_probe_is_cached(self):
        probe = AsyncMock(return_value=20000)
        config = {"max_tokens": 0, "provider": "lmstudio", "base_url": "http://x/v1", "model": "m"}
        with patch.object(providers, "context_window", new=probe):
            self.assertEqual(await providers.resolved_max_tokens(config), 10000)
            self.assertEqual(await providers.resolved_max_tokens(config), 10000)
        probe.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
