"""Offline tests for providers.py request/stream parsing, using a fake local server."""

from __future__ import annotations

import asyncio
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
        self.app.router.add_get("/v1/models", self._strata_models)
        self.app.router.add_get("/api/v1/models", self._lmstudio_loaded_models)
        self.app.router.add_post("/api/v1/models/unload", self._lmstudio_unload)
        self.app.router.add_get("/api/ps", self._ollama_loaded_models)
        self.app.router.add_post("/api/generate", self._ollama_unload)
        self.app.router.add_post("/v1/chat/completions", self._openai_stream)
        self.app.router.add_post("/embed/v1/embeddings", self._embedding_response)
        self.app.router.add_post("/v1/messages", self._anthropic_stream)
        self.app.router.add_post("/v1/bad/chat/completions", self._server_error)
        self.app.router.add_post("/v1/stream-error/chat/completions", self._stream_error)
        self.app.router.add_post("/v1/reasoning/chat/completions", self._openai_reasoning_stream)
        self.app.router.add_post("/v1/overflow/chat/completions", self._overflow_then_stream)
        self.app.router.add_post("/v1/summary-overflow/chat/completions", self._summary_overflow_then_json)
        self.app.router.add_post("/v1/noncontext/chat/completions", self._noncontext_error)
        self.app.router.add_post("/reasoning/v1/messages", self._anthropic_reasoning_stream)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.site = site
        port = site._server.sockets[0].getsockname()[1]
        self.root = f"http://127.0.0.1:{port}"
        self.base = f"{self.root}/v1"
        self.requests = []
        self.chat_bodies = []

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def _stream_error(self, request):
        return web.Response(text='data: {"error": {"message": "model failed"}}\n\n', content_type="text/event-stream")

    async def _lmstudio_loaded_models(self, _request):
        return web.json_response({"models": [
            {"key": "embed-target", "loaded_instances": [{"id": "instance-target"}]},
            {"key": "chat-model", "loaded_instances": [{"id": "instance-chat"}]},
        ]})

    async def _lmstudio_unload(self, request):
        payload = await request.json()
        self.requests.append((request.path, payload.get("instance_id")))
        return web.json_response({"success": True})

    async def _ollama_loaded_models(self, _request):
        return web.json_response({"models": [{"name": "embed-target"}, {"name": "chat-model"}]})

    async def _ollama_unload(self, request):
        payload = await request.json()
        self.requests.append((request.path, payload.get("model"), payload.get("keep_alive")))
        return web.json_response({"done": True})

    async def test_openai_in_stream_error_is_not_silent_success(self):
        events = [event async for event in providers._openai_chat(self._config(f"{self.base}/stream-error"), [], [])]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("model failed", events[0]["error"])

    async def test_chat_and_embedding_use_independent_endpoints_and_keys(self):
        chat = self._config(self.base, provider="openai", api_key="chat-secret")
        chat["model"] = "chat-model"
        chat["embedding"] = {"provider": "openai", "base_url": f"{self.root}/embed/v1", "api_key": "embed-secret"}
        events = [event async for event in providers.chat_events(chat, [{"role": "user", "content": "hi"}], [])]
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(await providers.embed(chat, ["text"], "embed-model"), [[1.0, 0.0]])
        self.assertEqual(self.requests, [("/v1/chat/completions", "Bearer chat-secret"),
                                         ("/embed/v1/embeddings", "Bearer embed-secret")])

    async def test_embedding_provider_blank_key_does_not_inherit_chat_secret(self):
        config = self._config(self.base, provider="openai", api_key="chat-secret")
        config["embedding"] = {"provider": "openai", "base_url": f"{self.root}/embed/v1", "api_key": ""}
        await providers.embed(config, ["text"], "embed-model")
        self.assertEqual(self.requests, [("/embed/v1/embeddings", None)])

    async def test_idle_unload_targets_only_selected_lm_studio_model(self):
        config = self._config(self.base, provider="lmstudio", api_key="")
        result = await providers.unload_embedding_model(config, "embed-target")
        self.assertEqual(result["unloaded"], ["instance-target"])
        self.assertEqual(self.requests, [("/api/v1/models/unload", "instance-target")])

    async def test_idle_unload_targets_only_selected_ollama_model(self):
        config = self._config(self.base, provider="ollama", api_key="")
        result = await providers.unload_embedding_model(config, "embed-target")
        self.assertEqual(result["unloaded"], ["embed-target"])
        self.assertEqual(self.requests, [("/api/generate", "embed-target", 0)])

    def test_embedding_identity_includes_endpoint_and_model(self):
        config = {"provider": "openai", "base_url": self.base,
                  "embedding": {"provider": "openai", "base_url": f"{self.root}/embed/v1", "api_key": "secret"}}
        first = providers.embedding_identity(config, "same-model")
        second = providers.embedding_identity({**config, "embedding": {**config["embedding"], "base_url": self.base}}, "same-model")
        self.assertNotEqual(first, second)
        self.assertNotIn("secret", first)
        upper_path = providers.embedding_identity({**config, "embedding": {**config["embedding"], "base_url": f"{self.root}/embed/A"}}, "same-model")
        lower_path = providers.embedding_identity({**config, "embedding": {**config["embedding"], "base_url": f"{self.root}/embed/a"}}, "same-model")
        self.assertNotEqual(upper_path, lower_path)

    def test_idle_unload_callback_skips_model_with_active_chat_lease(self):
        key = "lmstudio|http://same-endpoint/v1|shared-model"
        providers._idle_generations[key] = 97231
        providers._active_models[key] = 1
        providers._idle_configs[key] = ({"provider": "lmstudio", "base_url": "http://same-endpoint/v1"}, "shared-model")
        try:
            with patch("providers.asyncio.run") as run:
                providers._timer_unload(key, 97231)
            run.assert_not_called()
        finally:
            providers._active_models.pop(key, None)
            providers._idle_configs.pop(key, None)
            providers._idle_generations.pop(key, None)

    async def test_cancelled_activity_acquire_releases_late_thread_lease(self):
        key = "lmstudio|http://cancel-test/v1|model"
        config = {"provider": "lmstudio", "base_url": "http://cancel-test/v1", "limits": {"embedding_idle_unload_seconds": 0}}
        with providers._activity_condition:
            providers._unloading_models.add(key)
        task = asyncio.create_task(providers._acquire_model_activity(key, config, "model"))
        await asyncio.sleep(0)
        task.cancel()
        with providers._activity_condition:
            providers._unloading_models.discard(key)
            providers._activity_condition.notify_all()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(key, providers._active_models)

    # --- handlers ---------------------------------------------------------- #

    async def _openai_stream(self, request):
        self.requests.append((request.path, request.headers.get("Authorization")))
        self.chat_bodies.append(await request.json())
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

    async def _embedding_response(self, request):
        self.requests.append((request.path, request.headers.get("Authorization")))
        payload = await request.json()
        return web.json_response({"data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(payload["input"])]})

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

    async def _overflow_then_stream(self, request):
        body = await request.json()
        self.requests.append(body)
        if body["max_tokens"] > 253564:
            return web.json_response(
                {"error": {"message": "context length exceeded; max output tokens at most 253564"}},
                status=400,
            )
        return web.Response(
            text='data: {"choices":[{"delta":{"content":"fit"}}]}\n\ndata: [DONE]\n\n',
            content_type="text/event-stream",
        )

    async def _noncontext_error(self, request):
        body = await request.json()
        self.requests.append(body)
        return web.json_response({"error": {"message": "invalid max_tokens"}}, status=400)

    async def _summary_overflow_then_json(self, request):
        body = await request.json()
        self.requests.append(body)
        if body["max_tokens"] > 253564:
            return web.json_response(
                {"error": {"message": "context length exceeded; max output tokens at most 253564"}},
                status=400,
            )
        return web.json_response({"choices": [{"message": {"content": "summary"}}]})

    # --- tests ------------------------------------------------------------- #

    async def _strata_models(self, request):
        return web.json_response({"data": [
            {"id": "test-model", "meta": {"n_ctx": 262144},
             "architecture": {"input_modalities": ["text", "image"]}},
            {"id": "text-only", "architecture": {"input_modalities": ["text"]}},
            {"id": "unknown", "meta": None, "architecture": None},
        ]})

    async def test_strata_discovery_capabilities_and_streaming(self):
        config = self._config(self.base, provider="strata", api_key="")
        self.assertIn("test-model", await providers.list_models(config))
        self.assertEqual(await providers.context_window(config, "test-model"), 262144)
        self.assertTrue(await providers.vision_support(config, "test-model"))
        self.assertFalse(await providers.vision_support(config, "text-only"))
        for model in ("unknown", "missing"):
            self.assertIsNone(await providers.context_window(config, model))
            self.assertIsNone(await providers.vision_support(config, model))
        events = [event async for event in providers.chat_events(
            config, [{"role": "user", "content": "hi"}], [])]
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"), "Hello")
        call = next(e for e in events if e["type"] == "tool_call")
        self.assertEqual(json.loads(call["arguments"]), {"query": "sampler"})
        self.assertEqual(events[-1]["type"], "done")

    async def test_strata_missing_metadata_endpoint_is_unknown(self):
        config = self._config(f"{self.root}/missing", provider="strata")
        self.assertIsNone(await providers.context_window(config, "test-model"))
        self.assertIsNone(await providers.vision_support(config, "test-model"))

    async def test_strata_root_url_uses_v1_and_embedding_404_is_actionable(self):
        config = self._config(self.root, provider="strata")
        self.assertEqual(providers._base_url(config), f"{self.root}/v1")
        self.assertEqual(
            providers._base_url({**config, "base_url": f"{self.root}/v1/"}),
            f"{self.root}/v1",
        )
        self.assertEqual(
            providers._base_url({**config, "base_url": f"{self.root}/gateway"}),
            f"{self.root}/gateway",
        )
        with self.assertRaisesRegex(RuntimeError, "does not expose.*embedding endpoint"):
            await providers._embed_request(config, ["test input"], "text-embedding-embedder_collection")

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

    async def test_context_overflow_retries_once_with_provider_allowance(self):
        messages = [{"role": "user", "content": "preserve every input byte"}]
        tools = [{"type": "function", "function": {"name": "keep_tool"}}]
        events = [event async for event in providers._openai_chat(
            self._config(f"{self.root}/v1/overflow", max_tokens=256000), messages, tools
        )]
        self.assertEqual("".join(event.get("text", "") for event in events), "fit")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["max_tokens"], 256000)
        self.assertEqual(self.requests[1]["max_tokens"], 253564)
        for key in ("messages", "tools", "tool_choice", "stream", "temperature", "model"):
            self.assertEqual(self.requests[1][key], self.requests[0][key])

    async def test_noncontext_400_is_not_retried(self):
        events = [event async for event in providers._openai_chat(
            self._config(f"{self.root}/v1/noncontext"), [{"role": "user", "content": "hi"}], []
        )]
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("invalid max_tokens", events[0]["error"])

    async def test_summary_context_overflow_retries_without_changing_transcript(self):
        transcript = "earlier discussion " * 80
        result = await providers.summarize(
            self._config(f"{self.root}/v1/summary-overflow", max_tokens=256000), transcript
        )
        self.assertEqual(result, "summary")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["max_tokens"], 256000)
        self.assertEqual(self.requests[1]["max_tokens"], 253564)
        self.assertEqual(self.requests[1]["messages"], self.requests[0]["messages"])

    def test_malformed_context_overflow_is_not_retried(self):
        self.assertIsNone(providers._context_overflow_retry_tokens(
            400, "context length exceeded; allowance is not stated", 256000
        ))

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

    async def test_openai_reasoning_effort_is_sent_only_for_supported_openai_models(self):
        config = self._config("https://api.openai.com/v1", provider="openai", model="gpt-5.1",
                              thinking={"level": "low"})
        with patch.object(providers, "_base_url", return_value=self.base):
            events = [event async for event in providers._openai_chat(config, [], [])]
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.chat_bodies[-1]["reasoning_effort"], "low")
        self.assertIsNone(providers._openai_reasoning_effort(
            self._config(self.base, provider="strata", model="qwen3.8-flash-next-abliterated-iq3_xxs",
                         thinking={"level": "low"})
        ))
        self.assertIsNone(providers._openai_reasoning_effort(
            self._config("https://api.openai.com/v1", provider="openai", model="gpt-4.1",
                         thinking={"level": "high"})
        ))
        self.assertIsNone(providers._openai_reasoning_effort(
            self._config("https://api.openai.com/v1", provider="openai", model="gpt-5-pro",
                         thinking={"level": "low"})
        ))

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

    async def test_failed_window_probe_does_not_poison_forever_cache(self):
        probe = AsyncMock(side_effect=[None, 2048])
        config = {"provider": "lmstudio", "base_url": "http://test/v1"}
        with patch.object(providers, "context_window", new=probe):
            self.assertIsNone(await providers._cached_context_window(config, "m"))
            self.assertEqual(await providers._cached_context_window(config, "m"), 2048)
        self.assertEqual(probe.await_count, 2)

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
