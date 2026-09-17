from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, AsyncIterator, Mapping

import aiohttp

try:
    from .debug import debug_log as _debug_log
except ImportError:
    from debug import debug_log as _debug_log

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_FALLBACK_MAX_TOKENS = 8192

DATA_URL_RE = re.compile(r"^data:(?P<media>[^;,]+);base64,(?P<data>.*)$", re.S)


def _base_url(config: Mapping[str, Any]) -> str:
    return str(config.get("base_url") or "").rstrip("/")


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _thinking_enabled(config: Mapping[str, Any]) -> bool:
    thinking = config.get("thinking")
    if isinstance(thinking, Mapping):
        return thinking.get("enabled", True) is not False
    return True


def _timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=None)


def _is_anthropic(config: Mapping[str, Any]) -> bool:
    return config.get("provider") == "anthropic"


def _headers(config: Mapping[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = str(config.get("api_key") or "").strip()
    if _is_anthropic(config):
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if token:
            headers["x-api-key"] = token
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _read_error(response: aiohttp.ClientResponse) -> str:
    detail = await response.text()
    return f"Provider returned HTTP {response.status}: {detail[:1000]}"


async def list_models(config: Mapping[str, Any]) -> list[str]:
    base = _base_url(config)
    if _is_anthropic(config):
        url = f"{base}/v1/models?limit=100"
    else:
        url = f"{base}/models"
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        async with session.get(url, headers=_headers(config)) as response:
            if response.status >= 400:
                raise RuntimeError(await _read_error(response))
            payload = await response.json()
    items = payload.get("data", payload.get("models", [])) if isinstance(payload, Mapping) else []
    models = []
    for item in items:
        if isinstance(item, Mapping):
            model_id = item.get("id") or item.get("name")
            if model_id:
                models.append(str(model_id))
    return models


def _tool_protocol(tools: list[Mapping[str, Any]]) -> str:
    lines = [
        "You do not have native tool calling. To use a tool, reply with a fenced JSON block:",
        "```json",
        '{"tool": "<name>", "arguments": { ... }}',
        "```",
        "You may output plain text alongside one or more tool blocks. After the tool runs you will",
        "receive its result and can continue. Available tools:",
    ]
    for tool in tools:
        function = tool.get("function", tool)
        lines.append(f"- {function.get('name')}: {function.get('description', '')}")
    return "\n".join(lines)


def _with_system_protocol(
    messages: list[Mapping[str, Any]], tools: list[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    protocol = _tool_protocol(tools)
    prepared = list(messages)
    for index, message in enumerate(prepared):
        if message.get("role") == "system":
            prepared[index] = {"role": "system", "content": f"{message.get('content', '')}\n\n{protocol}"}
            return prepared
    return [{"role": "system", "content": protocol}, *prepared]


def _anthropic_tools(tools: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        function = tool.get("function", tool)
        converted.append(
            {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return converted


def _anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return str(content)
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        part_type = part.get("type")
        if part_type == "text":
            blocks.append({"type": "text", "text": str(part.get("text", ""))})
        elif part_type == "image_url":
            url = str((part.get("image_url") or {}).get("url", ""))
            match = DATA_URL_RE.match(url)
            if match:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": match.group("media"),
                            "data": match.group("data"),
                        },
                    }
                )
    return blocks or str(content)


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    return content if isinstance(content, list) else [{"type": "text", "text": str(content)}]


def _anthropic_messages(messages: list[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(str(message.get("content", "")))
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": str(message.get("content", "")),
            }
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": str(message["content"])})
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "input": arguments,
                    }
                )
            converted.append({"role": "assistant", "content": blocks or str(message.get("content", ""))})
            continue
        converted.append({"role": "user", "content": _anthropic_content(message.get("content", ""))})
    merged: list[dict[str, Any]] = []
    for message in converted:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"] = _content_blocks(merged[-1]["content"]) + _content_blocks(message["content"])
        else:
            merged.append(message)
    return "\n\n".join(part for part in system_parts if part), merged


async def _openai_chat(
    config: Mapping[str, Any], messages: list[Mapping[str, Any]], tools: list[Mapping[str, Any]]
) -> AsyncIterator[dict[str, Any]]:
    max_tokens = _positive_int(config.get("max_tokens"))
    body: dict[str, Any] = {
        "model": config.get("model"),
        "messages": messages,
        "stream": True,
        "temperature": config.get("temperature", 0.7),
    }
    # Auto mode resolves to half the model's context window; when the window is unknown the
    # field is omitted so the provider applies its own default.
    if max_tokens:
        body["max_tokens"] = max_tokens
    if tools and config.get("use_native_tools", True):
        body["tools"] = tools
        body["tool_choice"] = "auto"
    url = f"{_base_url(config)}/chat/completions"
    thinking = _thinking_enabled(config)
    pending: dict[int, dict[str, str]] = {}
    finish_reason = None
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        async with session.post(url, headers=_headers(config), json=body) as response:
            if response.status >= 400:
                yield {"type": "error", "error": await _read_error(response)}
                return
            async for raw_line in response.content:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    yield {"type": "error", "error": str(event["error"])}
                    return
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {}) or {}
                if thinking:
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        yield {"type": "reasoning", "text": reasoning}
                if delta.get("content"):
                    yield {"type": "text", "text": delta["content"]}
                for call in delta.get("tool_calls") or []:
                    index = call.get("index", 0)
                    slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if call.get("id"):
                        slot["id"] = call["id"]
                    function = call.get("function") or {}
                    if function.get("name"):
                        slot["name"] = function["name"]
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    for index in sorted(pending):
        slot = pending[index]
        if slot["name"]:
            yield {"type": "tool_call", "id": slot["id"], "name": slot["name"], "arguments": slot["arguments"]}
    yield {"type": "done", "finish_reason": finish_reason}


async def _anthropic_chat(
    config: Mapping[str, Any], messages: list[Mapping[str, Any]], tools: list[Mapping[str, Any]]
) -> AsyncIterator[dict[str, Any]]:
    system, anthropic_messages = _anthropic_messages(messages)
    # Anthropic requires an explicit output limit. Auto mode resolves to half the context window;
    # a direct call with no limit falls back to a sane default.
    max_tokens = _positive_int(config.get("max_tokens")) or ANTHROPIC_FALLBACK_MAX_TOKENS
    body: dict[str, Any] = {
        "model": config.get("model"),
        "messages": anthropic_messages,
        "stream": True,
        "temperature": config.get("temperature", 0.7),
        "max_tokens": max_tokens,
    }
    if system:
        body["system"] = system
    if tools and config.get("use_native_tools", True):
        body["tools"] = _anthropic_tools(tools)
    url = f"{_base_url(config)}/v1/messages"
    thinking = _thinking_enabled(config)
    current_tool: dict[str, Any] | None = None
    finish_reason = None
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        async with session.post(url, headers=_headers(config), json=body) as response:
            if response.status >= 400:
                yield {"type": "error", "error": await _read_error(response)}
                return
            async for raw_line in response.content:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        current_tool = {"id": block.get("id"), "name": block.get("name"), "arguments": ""}
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield {"type": "text", "text": delta.get("text", "")}
                    elif delta.get("type") == "thinking_delta" and thinking:
                        yield {"type": "reasoning", "text": delta.get("thinking", "")}
                    elif delta.get("type") == "input_json_delta" and current_tool is not None:
                        current_tool["arguments"] += delta.get("partial_json", "")
                elif event_type == "content_block_stop":
                    if current_tool is not None:
                        yield {"type": "tool_call", **current_tool}
                        current_tool = None
                elif event_type == "message_delta":
                    finish_reason = event.get("delta", {}).get("stop_reason", finish_reason)
                elif event_type == "error":
                    yield {"type": "error", "error": str(event.get("error", event))}
                    return
    yield {"type": "done", "finish_reason": finish_reason}


async def chat_events(
    config: Mapping[str, Any], messages: list[Mapping[str, Any]], tools: list[Mapping[str, Any]]
) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    first_text: float | None = None
    first_reasoning: float | None = None
    if not config.get("model"):
        yield {"type": "error", "error": "No model selected. Open settings and choose a model."}
        return
    max_tokens = await resolved_max_tokens(config)
    config = {**config, "max_tokens": max_tokens}
    _debug_log(
        "provider",
        "chat.start",
        provider=config.get("provider"),
        model=config.get("model"),
        messages=len(messages),
        tools=len(tools),
        native_tools=config.get("use_native_tools", True),
        temperature=config.get("temperature"),
        max_tokens=max_tokens,
    )
    if not config.get("use_native_tools", True) and tools:
        messages = _with_system_protocol(messages, tools)

    async def _relay() -> AsyncIterator[dict[str, Any]]:
        nonlocal first_text, first_reasoning
        if _is_anthropic(config):
            stream = _anthropic_chat(config, messages, tools)
        else:
            stream = _openai_chat(config, messages, tools)
        async for event in stream:
            kind = event.get("type")
            if kind == "reasoning" and first_reasoning is None:
                first_reasoning = time.time()
                _debug_log("provider", "chat.first_reasoning", ms=round((first_reasoning - started) * 1000))
            elif kind == "text" and first_text is None:
                first_text = time.time()
                _debug_log("provider", "chat.first_token", ms=round((first_text - started) * 1000))
            elif kind == "done":
                _debug_log(
                    "provider",
                    "chat.done",
                    finish_reason=event.get("finish_reason"),
                    ms=round((time.time() - started) * 1000),
                )
            elif kind == "error":
                _debug_log("provider", "chat.error", level="error", error=event.get("error"))
            yield event

    try:
        async for event in _relay():
            yield event
    except aiohttp.ClientError as exc:
        _debug_log("provider", "chat.error", level="error", error=f"client error: {exc}")
        yield {"type": "error", "error": f"Could not reach provider: {exc}"}
    except asyncio.TimeoutError:
        _debug_log("provider", "chat.error", level="error", error="timeout")
        yield {"type": "error", "error": "Provider request timed out."}


def _api_root(config: Mapping[str, Any]) -> str:
    base = _base_url(config)
    return base[:-3] if base.endswith("/v1") else base


_VISION_HINTS = (
    "vl", "vision", "llava", "pixtral", "internvl", "minicpm-v", "gemma-3",
    "gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4",
)


async def vision_support(config: Mapping[str, Any], model: str) -> bool | None:
    provider = config.get("provider")
    model = (model or "").strip()
    if not model:
        return None
    if provider == "anthropic":
        return True
    root = _api_root(config)
    if provider == "lmstudio":
        try:
            async with aiohttp.ClientSession(timeout=_timeout()) as session:
                async with session.get(f"{root}/api/v0/models", headers=_headers(config)) as response:
                    if response.status < 400:
                        payload = await response.json()
                        for item in payload.get("data", []) if isinstance(payload, Mapping) else []:
                            if isinstance(item, Mapping) and item.get("id") == model:
                                return str(item.get("type", "")).lower() == "vlm"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        return None
    if provider == "ollama":
        try:
            async with aiohttp.ClientSession(timeout=_timeout()) as session:
                async with session.post(f"{root}/api/show", headers=_headers(config), json={"model": model}) as response:
                    if response.status < 400:
                        payload = await response.json()
                        capabilities = payload.get("capabilities") if isinstance(payload, Mapping) else []
                        return "vision" in (capabilities or [])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        return None
    lowered = model.lower()
    if any(hint in lowered for hint in _VISION_HINTS):
        return True
    return None


SUMMARY_SYSTEM = (
    "You compress conversation history for a ComfyUI assistant. Summarize the transcript concisely, "
    "preserving the user's goals and preferences, workflow and node decisions, node ids and their "
    "roles, prompts written, problems found and fixes applied, and any pending tasks. Use short "
    "bullet points. Do not invent details that are not in the transcript."
)


async def context_window(config: Mapping[str, Any], model: str) -> int | None:
    provider = config.get("provider")
    if provider == "anthropic":
        return 200000
    if not model:
        return None
    root = _api_root(config)
    if provider == "lmstudio":
        try:
            async with aiohttp.ClientSession(timeout=_timeout()) as session:
                async with session.get(f"{root}/api/v0/models", headers=_headers(config)) as response:
                    if response.status < 400:
                        payload = await response.json()
                        for item in payload.get("data", []) if isinstance(payload, Mapping) else []:
                            if isinstance(item, Mapping) and item.get("id") == model:
                                value = item.get("loaded_context_length") or item.get("max_context_length")
                                return int(value) if value else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        return None
    if provider == "ollama":
        try:
            async with aiohttp.ClientSession(timeout=_timeout()) as session:
                async with session.post(f"{root}/api/show", headers=_headers(config), json={"model": model}) as response:
                    if response.status < 400:
                        payload = await response.json()
                        info = payload.get("model_info") if isinstance(payload, Mapping) else {}
                        for key, value in (info or {}).items():
                            if key.endswith(".context_length"):
                                return int(value)
                        if isinstance(info, Mapping) and info.get("context_length"):
                            return int(info["context_length"])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        return None
    return None


_WINDOW_TTL_SECONDS = 300
_window_cache: dict[tuple[Any, str, str], tuple[float, int | None]] = {}


async def _cached_context_window(config: Mapping[str, Any], model: str) -> int | None:
    key = (config.get("provider"), _base_url(config), str(model or ""))
    now = time.time()
    cached = _window_cache.get(key)
    if cached and now - cached[0] < _WINDOW_TTL_SECONDS:
        return cached[1]
    try:
        window = await context_window(config, model)
    except Exception:
        window = None
    _window_cache[key] = (now, window)
    return window


async def resolved_max_tokens(config: Mapping[str, Any]) -> int | None:
    """Output cap for a request: an explicit setting wins, otherwise half the context window.

    Returns None when neither is known, so callers can omit the field and let the provider
    apply its own default.
    """
    explicit = _positive_int(config.get("max_tokens"))
    if explicit:
        return explicit
    context = config.get("context") or {}
    window = _positive_int(context.get("window") if isinstance(context, Mapping) else 0)
    if not window:
        window = _positive_int(await _cached_context_window(config, str(config.get("model") or "")))
    return window // 2 if window else None


async def summarize(config: Mapping[str, Any], text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    max_tokens = await resolved_max_tokens(config)
    if _is_anthropic(config):
        max_tokens = max_tokens or ANTHROPIC_FALLBACK_MAX_TOKENS
        body = {
            "model": config.get("model"),
            "system": SUMMARY_SYSTEM,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        url = f"{_base_url(config)}/v1/messages"
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            async with session.post(url, headers=_headers(config), json=body) as response:
                if response.status >= 400:
                    raise RuntimeError(await _read_error(response))
                payload = await response.json()
        parts = [block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"]
        return "".join(parts).strip()
    body = {
        "model": config.get("model"),
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": text},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    url = f"{_base_url(config)}/chat/completions"
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        async with session.post(url, headers=_headers(config), json=body) as response:
            if response.status >= 400:
                raise RuntimeError(await _read_error(response))
            payload = await response.json()
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message", {})
    return str(message.get("content") or "").strip()


_EMBED_HINTS = ("embed", "bge", "nomic", "mxbai", "minilm", "gte", "e5", "text-embedding")


async def embedding_models(config: Mapping[str, Any]) -> list[str]:
    """Return embedding-capable model ids for the configured provider."""
    provider = config.get("provider")
    root = _api_root(config)
    if provider == "lmstudio":
        try:
            async with aiohttp.ClientSession(timeout=_timeout()) as session:
                async with session.get(f"{root}/api/v0/models", headers=_headers(config)) as response:
                    if response.status < 400:
                        payload = await response.json()
                        if isinstance(payload, Mapping):
                            return [
                                str(item.get("id"))
                                for item in payload.get("data", [])
                                if isinstance(item, Mapping) and str(item.get("type", "")).lower() == "embeddings"
                            ]
        except (aiohttp.ClientError, ValueError):
            return []
        return []
    if provider == "anthropic":
        return []
    try:
        models = await list_models(config)
    except Exception:
        return []
    return [model for model in models if any(hint in model.lower() for hint in _EMBED_HINTS)]


async def resolve_embedding_model(config: Mapping[str, Any], explicit: str = "") -> str:
    """Pick an embedding model: the configured one, else an auto-detected candidate."""
    model = str(explicit or config.get("embed_model") or "").strip()
    if model:
        return model
    try:
        candidates = await embedding_models(config)
    except Exception:
        return ""
    if not candidates:
        return ""
    for candidate in candidates:
        if "nomic" in candidate.lower():
            return candidate
    return candidates[0]


def _embedding_vectors(payload: Any) -> list[list[float]]:
    items = payload.get("data", []) if isinstance(payload, Mapping) else []
    ordered = sorted(items, key=lambda item: item.get("index", 0) if isinstance(item, Mapping) else 0)
    return [list(item.get("embedding", [])) for item in ordered if isinstance(item, Mapping)]


async def _ollama_embed(session: aiohttp.ClientSession, config: Mapping[str, Any], texts: list[str], model: str) -> list[list[float]]:
    url = f"{_api_root(config)}/api/embed"
    async with session.post(url, headers=_headers(config), json={"model": model, "input": texts}) as response:
        if response.status >= 400:
            raise RuntimeError(await _read_error(response))
        payload = await response.json()
    return [list(vector) for vector in (payload.get("embeddings", []) if isinstance(payload, Mapping) else [])]


async def embed(config: Mapping[str, Any], texts: list[str], model: str = "") -> list[list[float]]:
    texts = [text for text in texts if text and text.strip()]
    if not texts:
        return []
    model = str(model or config.get("embed_model") or "").strip()
    url = f"{_base_url(config)}/embeddings"
    body = {"model": model, "input": texts}
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        async with session.post(url, headers=_headers(config), json=body) as response:
            if response.status >= 400:
                if config.get("provider") == "ollama":
                    return await _ollama_embed(session, config, texts, model)
                raise RuntimeError(await _read_error(response))
            payload = await response.json()
    vectors = _embedding_vectors(payload)
    if not vectors and config.get("provider") == "ollama":
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            return await _ollama_embed(session, config, texts, model)
    return vectors


async def unload_models(config: Mapping[str, Any]) -> dict[str, Any]:
    provider = config.get("provider")
    root = _api_root(config)
    unloaded: list[str] = []
    if provider == "lmstudio":
        url = f"{root}/api/v1/models"
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            async with session.get(url, headers=_headers(config)) as response:
                if response.status == 404:
                    return {"unsupported": True, "unloaded": [], "reason": "LM Studio v1 API is not available (needs 0.4.0+)."}
                if response.status >= 400:
                    raise RuntimeError(await _read_error(response))
                payload = await response.json()
            models = payload.get("models", []) if isinstance(payload, Mapping) else []
            for model in models:
                if not isinstance(model, Mapping):
                    continue
                for instance in model.get("loaded_instances") or []:
                    instance_id = str(instance.get("id") or "").strip() if isinstance(instance, Mapping) else ""
                    if not instance_id:
                        continue
                    async with session.post(
                        f"{root}/api/v1/models/unload",
                        headers=_headers(config),
                        json={"instance_id": instance_id},
                    ) as unload_response:
                        if unload_response.status < 400:
                            unloaded.append(instance_id)
        return {"unsupported": False, "unloaded": unloaded}
    if provider == "ollama":
        url = f"{root}/api/ps"
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            async with session.get(url, headers=_headers(config)) as response:
                if response.status >= 400:
                    raise RuntimeError(await _read_error(response))
                payload = await response.json()
            models = payload.get("models", []) if isinstance(payload, Mapping) else []
            for model in models:
                name = str(model.get("name") or model.get("model") or "").strip() if isinstance(model, Mapping) else ""
                if not name:
                    continue
                async with session.post(
                    f"{root}/api/generate",
                    headers=_headers(config),
                    json={"model": name, "keep_alive": 0},
                ) as unload_response:
                    if unload_response.status < 400:
                        unloaded.append(name)
        return {"unsupported": False, "unloaded": unloaded}
    return {"unsupported": True, "unloaded": [], "reason": f"Provider '{provider}' has nothing to unload."}
