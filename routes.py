from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Mapping

from aiohttp import web
from server import PromptServer

from . import console, debug, experience, kb, memory, providers, websearch
from .config_store import CONFIG_STORE

routes = PromptServer.instance.routes


async def _json_object(request: web.Request) -> dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "JSON body must be an object."}),
            content_type="application/json",
        )
    return payload


def _effective_config(payload: Any) -> dict[str, Any]:
    config = CONFIG_STORE.resolved()
    if isinstance(payload, Mapping):
        for key in ("provider", "base_url", "model"):
            if payload.get(key):
                config[key] = payload[key]
        if payload.get("api_key"):
            config["api_key"] = payload["api_key"]
        if isinstance(payload.get("websearch"), Mapping):
            for key, value in payload["websearch"].items():
                if value:
                    config.setdefault("websearch", {})[key] = value
    return config


def _limit_value(payload: Mapping[str, Any], key: str, default: int = 0) -> int:
    """Read a caller-supplied limit. 0 is preserved (it means no cap)."""
    value = payload.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _memory_embed_settings(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    memory_config = config.get("memory") or {}
    embed = memory_config.get("embed") or {}
    if memory_config.get("enabled", True) is False or embed.get("enabled", True) is False:
        return None
    return embed


async def _lesson_vector(config: Mapping[str, Any], text: str) -> list[float] | None:
    """Best-effort embedding for a lesson or query. Never raises into the request."""
    embed = _memory_embed_settings(config)
    text = str(text or "").strip()
    if embed is None or not text:
        return None
    model = await providers.resolve_embedding_model(config, str(embed.get("model") or ""))
    if not model:
        return None
    await asyncio.to_thread(memory.ensure_embed_model, model)
    try:
        _tokens, budget_chars, _source = await providers.embedding_limits(config, model)
        if budget_chars > 0:
            text = text[:budget_chars]
    except Exception:
        pass
    try:
        vectors = await providers.embed(config, [text], model)
    except Exception as exc:
        await asyncio.to_thread(memory.set_embed_error, str(exc))
        return None
    return vectors[0] if vectors else None


def _experience_embed_text(record: Mapping[str, Any]) -> str:
    parts = [str(record.get("task") or ""), " ".join(experience._node_types(record.get("fragment")))]
    if record.get("error"):
        parts.append(str(record["error"]))
    return " ".join(part for part in parts if part).strip()


async def _experience_vector(config: Mapping[str, Any], text: str) -> list[float] | None:
    """Experience retrieval reuses the memory embedding settings."""
    embed = _memory_embed_settings(config)
    text = str(text or "").strip()
    if embed is None or not text:
        return None
    model = await providers.resolve_embedding_model(config, str(embed.get("model") or ""))
    if not model:
        return None
    await asyncio.to_thread(experience.ensure_embed_model, model)
    try:
        _tokens, budget_chars, _source = await providers.embedding_limits(config, model)
        if budget_chars > 0:
            text = text[:budget_chars]
    except Exception:
        pass
    try:
        vectors = await providers.embed(config, [text], model)
    except Exception as exc:
        await asyncio.to_thread(experience.set_embed_error, str(exc))
        return None
    return vectors[0] if vectors else None


@routes.get("/chatbot/config")
async def get_config(_request: web.Request) -> web.Response:
    return web.json_response({"config": CONFIG_STORE.snapshot(include_secrets=False)})


@routes.post("/chatbot/config")
async def set_config(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
        return web.json_response({"config": CONFIG_STORE.update(payload)})
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


@routes.get("/chatbot/history")
async def get_history(request: web.Request) -> web.Response:
    key = request.query.get("key") or "__default__"
    return web.json_response({"history": CONFIG_STORE.history(key)})


@routes.post("/chatbot/history")
async def set_history(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    key = str(payload.get("key") or "__default__")
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return web.json_response({"error": "Messages must be a JSON array."}, status=400)
    CONFIG_STORE.set_history(key, messages)
    return web.json_response({"ok": True})


@routes.post("/chatbot/models")
async def list_models(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    try:
        models = await providers.list_models(_effective_config(payload))
        return web.json_response({"models": models})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


@routes.post("/chatbot/embed_models")
async def embed_models(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    try:
        models = await providers.embedding_models(_effective_config(payload))
        return web.json_response({"models": models})
    except Exception as exc:
        return web.json_response({"models": [], "error": str(exc)})


@routes.post("/chatbot/vision")
async def vision_support(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    config = _effective_config(payload)
    model = str(payload.get("model") or config.get("model") or "").strip()
    try:
        result = await providers.vision_support(config, model)
        return web.json_response({"vision": result})
    except Exception as exc:
        return web.json_response({"vision": None, "error": str(exc)})


@routes.post("/chatbot/context")
async def context_window(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    config = _effective_config(payload)
    model = str(payload.get("model") or config.get("model") or "").strip()
    try:
        window = await providers.context_window(config, model)
        return web.json_response({"window": window})
    except Exception as exc:
        return web.json_response({"window": None, "error": str(exc)})


@routes.post("/chatbot/summarize")
async def summarize(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    text = str(payload.get("text") or "")
    if not text.strip():
        return web.json_response({"summary": ""})
    try:
        summary = await providers.summarize(_effective_config(payload), text)
        return web.json_response({"summary": summary})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


@routes.post("/chatbot/chat")
async def chat(request: web.Request) -> web.StreamResponse:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    messages = payload.get("messages", [])
    tools = payload.get("tools", [])
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        return web.json_response({"error": "Messages must be an array of objects."}, status=400)
    if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
        return web.json_response({"error": "Tools must be an array of objects."}, status=400)
    config = _effective_config(payload)
    debug.log(
        "route",
        "chat.request",
        provider=config.get("provider"),
        model=config.get("model"),
        messages=len(messages),
        tools=len(tools),
        native_tools=config.get("use_native_tools", True),
    )

    response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-cache"})
    await response.prepare(request)
    try:
        async for event in providers.chat_events(config, messages, tools):
            try:
                await response.write((json.dumps(event) + "\n").encode("utf-8"))
            except ConnectionResetError:
                debug.log("route", "chat.disconnect")
                return response
    except ConnectionResetError:
        debug.log("route", "chat.disconnect")
        return response
    except Exception as exc:
        debug.log("route", "chat.error", level="error", error=str(exc))
        try:
            await response.write((json.dumps({"type": "error", "error": str(exc)}) + "\n").encode("utf-8"))
        except ConnectionResetError:
            return response
    try:
        await response.write_eof()
    except ConnectionResetError:
        pass
    return response


@routes.post("/chatbot/websearch")
async def web_search(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    query = str(payload.get("query") or "").strip()
    if not query:
        return web.json_response({"error": "Missing search query."}, status=400)
    try:
        results = await websearch.search(_effective_config(payload), query, payload.get("count"))
        return web.json_response({"results": results})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


@routes.get("/chatbot/kb/status")
async def kb_status(_request: web.Request) -> web.Response:
    return web.json_response({"status": kb.status()})


@routes.post("/chatbot/kb/compact")
async def kb_compact(_request: web.Request) -> web.Response:
    try:
        result = await asyncio.to_thread(kb.compact, True)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"result": result, "status": kb.status()})


@routes.post("/chatbot/kb/rebuild")
async def kb_rebuild(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    config = _effective_config(payload).get("kb", {})
    auto_official = bool(payload.get("auto_official", config.get("auto_official", True)))
    refresh_days = int(payload.get("refresh_days", config.get("refresh_days", 7)))
    kb.start_background(force=True, auto_official=auto_official, refresh_days=refresh_days, force_official=True)
    return web.json_response({"status": kb.status()})


@routes.post("/chatbot/kb/sync_official")
async def kb_sync_official(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    config = _effective_config(payload).get("kb", {})
    refresh_days = int(payload.get("refresh_days", config.get("refresh_days", 7)))
    kb.start_background(auto_official=True, refresh_days=refresh_days, force_official=True)
    return web.json_response({"status": kb.status()})


@routes.post("/chatbot/docs/search")
async def docs_search(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    query = str(payload.get("query") or "").strip()
    if not query:
        return web.json_response({"error": "Missing query."}, status=400)
    source = payload.get("source") or None
    if source not in (None, "pack", "official", "node", "example", "registry", "model"):
        source = None
    try:
        results = await asyncio.to_thread(kb.search, query, _limit_value(payload, "limit", 0), source)
        return web.json_response({"results": results})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


@routes.post("/chatbot/docs/node")
async def docs_node(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    node_type = str(payload.get("type") or "").strip()
    if not node_type:
        return web.json_response({"error": "Missing node type."}, status=400)
    try:
        result = await asyncio.to_thread(kb.node_docs, node_type, _limit_value(payload, "limit", 0))
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


@routes.get("/chatbot/memory")
async def memory_list(_request: web.Request) -> web.Response:
    lessons = await asyncio.to_thread(memory.list_lessons)
    embedding = await asyncio.to_thread(memory.vector_stats)
    return web.json_response({"lessons": lessons, "embedding": embedding})


@routes.post("/chatbot/memory")
async def memory_update(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    action = str(payload.get("action") or "add")
    config = _effective_config(payload)
    try:
        if action == "add":
            text = str(payload.get("text") or "")
            vector = await _lesson_vector(config, text)
            result = await asyncio.to_thread(
                memory.add_lesson,
                text,
                str(payload.get("tags") or ""),
                str(payload.get("source") or "user"),
                bool(payload.get("pinned")),
                vector,
            )
        elif action == "update":
            ok = await asyncio.to_thread(
                memory.update_lesson,
                int(payload["id"]),
                payload.get("text"),
                payload.get("tags"),
                payload.get("enabled"),
                payload.get("pinned"),
            )
            if ok and payload.get("text"):
                vector = await _lesson_vector(config, str(payload["text"]))
                if vector is not None:
                    await asyncio.to_thread(memory.set_vector, int(payload["id"]), vector)
            result = {"ok": ok}
        elif action == "delete":
            result = {"ok": await asyncio.to_thread(memory.delete_lesson, int(payload["id"]))}
        elif action == "clear":
            await asyncio.to_thread(memory.clear_lessons)
            result = {"ok": True}
        else:
            return web.json_response({"error": f"Unknown action: {action}"}, status=400)
    except (KeyError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    lessons = await asyncio.to_thread(memory.list_lessons)
    return web.json_response({"result": result, "lessons": lessons})


@routes.post("/chatbot/memory/search")
async def memory_search(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    query = str(payload.get("query") or "")
    vector = await _lesson_vector(_effective_config(payload), query)
    lessons = await asyncio.to_thread(memory.search, query, _limit_value(payload, "limit", 0), vector)
    return web.json_response({"lessons": lessons})


@routes.post("/chatbot/memory/relevant")
async def memory_relevant(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    query = str(payload.get("query") or "")
    vector = await _lesson_vector(_effective_config(payload), query)
    lessons = await asyncio.to_thread(memory.relevant, query, _limit_value(payload, "limit", 0), vector)
    return web.json_response({"lessons": lessons})


@routes.post("/chatbot/memory/reindex")
async def memory_reindex(_request: web.Request) -> web.Response:
    config = CONFIG_STORE.resolved()
    if _memory_embed_settings(config) is None:
        return web.json_response({"indexed": 0, "reason": "disabled", "embedding": await asyncio.to_thread(memory.vector_stats)})
    model = await providers.resolve_embedding_model(config, str((config.get("memory", {}).get("embed") or {}).get("model") or ""))
    if not model:
        return web.json_response({"indexed": 0, "reason": "no embedding model", "embedding": await asyncio.to_thread(memory.vector_stats)})
    await asyncio.to_thread(memory.ensure_embed_model, model)
    pending = await asyncio.to_thread(memory.pending_lessons)
    indexed = 0
    try:
        for lesson_id, text in pending:
            vectors = await providers.embed(config, [text], model)
            if vectors and vectors[0]:
                await asyncio.to_thread(memory.set_vector, lesson_id, vectors[0])
                indexed += 1
        await asyncio.to_thread(memory.set_embed_error, "")
    except Exception as exc:
        await asyncio.to_thread(memory.set_embed_error, str(exc))
        return web.json_response(
            {"indexed": indexed, "error": str(exc), "embedding": await asyncio.to_thread(memory.vector_stats)},
            status=502,
        )
    return web.json_response({"indexed": indexed, "embedding": await asyncio.to_thread(memory.vector_stats)})


@routes.get("/chatbot/experience")
async def experience_list(_request: web.Request) -> web.Response:
    experiences = await asyncio.to_thread(experience.list_experiences)
    embedding = await asyncio.to_thread(experience.vector_stats)
    return web.json_response({"experiences": experiences, "embedding": embedding})


@routes.post("/chatbot/experience")
async def experience_record(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    fragment = payload.get("fragment") if isinstance(payload.get("fragment"), dict) else {}
    record = {
        "kind": "success" if str(payload.get("kind") or "") == "success" else "failure",
        "task": str(payload.get("task") or ""),
        "workflow_key": str(payload.get("workflow_key") or ""),
        "prompt_id": str(payload.get("prompt_id") or ""),
        "fragment": fragment,
        "models": payload.get("models") if isinstance(payload.get("models"), list) else [],
        "execution_status": str(payload.get("execution_status") or ""),
        "error": debug.scrub_text(payload.get("error") or ""),
        "failure_kind": str(payload.get("failure_kind") or ""),
        "node_errors": payload.get("node_errors"),
        "source": "run",
    }
    try:
        result = await asyncio.to_thread(experience.add_experience, record)
    except (TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    vector = await _experience_vector(_effective_config(payload), _experience_embed_text(record))
    if vector is not None:
        await asyncio.to_thread(experience.set_vector, result["id"], vector)
    return web.json_response({"result": result})


@routes.post("/chatbot/experience/verdict")
async def experience_verdict(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    try:
        ok = await asyncio.to_thread(
            experience.update_verdict,
            int(payload["id"]),
            str(payload.get("verdict") or ""),
            str(payload.get("note") or ""),
            str(payload.get("failure_kind") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"ok": ok})


@routes.post("/chatbot/experience/manage")
async def experience_manage(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    action = str(payload.get("action") or "")
    try:
        if action == "delete":
            await asyncio.to_thread(experience.delete_experience, int(payload["id"]))
        elif action in ("enable", "disable"):
            await asyncio.to_thread(experience.set_enabled, int(payload["id"]), action == "enable")
        elif action == "clear":
            await asyncio.to_thread(experience.clear_experiences)
        else:
            return web.json_response({"error": f"Unknown action: {action}"}, status=400)
    except (KeyError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    experiences = await asyncio.to_thread(experience.list_experiences)
    return web.json_response({"ok": True, "experiences": experiences})


@routes.post("/chatbot/experience/prune")
async def experience_prune(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    keep = _limit_value(payload, "keep", 0)
    removed = await asyncio.to_thread(experience.prune, keep)
    experiences = await asyncio.to_thread(experience.list_experiences)
    return web.json_response({"removed": removed, "experiences": experiences})


@routes.post("/chatbot/experience/search")
async def experience_search(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    query = str(payload.get("query") or "")
    vector = await _experience_vector(_effective_config(payload), query)
    results = await asyncio.to_thread(experience.search, query, _limit_value(payload, "limit", 0), vector)
    return web.json_response({"experiences": results})


@routes.post("/chatbot/experience/relevant")
async def experience_relevant(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    task = str(payload.get("task") or payload.get("query") or "")
    vector = await _experience_vector(_effective_config(payload), task)
    results = await asyncio.to_thread(experience.relevant, task, _limit_value(payload, "limit", 0), vector)
    for item in results:
        item["revalidate"] = await asyncio.to_thread(
            experience.revalidate, item.get("fragment"), item.get("pack_fingerprints")
        )
    return web.json_response({"experiences": results})


@routes.post("/chatbot/experience/reindex")
async def experience_reindex(_request: web.Request) -> web.Response:
    config = CONFIG_STORE.resolved()
    if _memory_embed_settings(config) is None:
        return web.json_response(
            {"indexed": 0, "reason": "disabled", "embedding": await asyncio.to_thread(experience.vector_stats)}
        )
    embed = config.get("memory", {}).get("embed", {}) or {}
    model = await providers.resolve_embedding_model(config, str(embed.get("model") or ""))
    if not model:
        return web.json_response(
            {"indexed": 0, "reason": "no embedding model", "embedding": await asyncio.to_thread(experience.vector_stats)}
        )
    await asyncio.to_thread(experience.ensure_embed_model, model)
    pending = await asyncio.to_thread(experience.pending_experiences)
    indexed = 0
    try:
        for experience_id, text in pending:
            if not text.strip():
                continue
            vectors = await providers.embed(config, [text], model)
            if vectors and vectors[0]:
                await asyncio.to_thread(experience.set_vector, experience_id, vectors[0])
                indexed += 1
        await asyncio.to_thread(experience.set_embed_error, "")
    except Exception as exc:
        await asyncio.to_thread(experience.set_embed_error, str(exc))
        return web.json_response(
            {"indexed": indexed, "error": str(exc), "embedding": await asyncio.to_thread(experience.vector_stats)},
            status=502,
        )
    return web.json_response({"indexed": indexed, "embedding": await asyncio.to_thread(experience.vector_stats)})


@routes.post("/chatbot/debug/report")
async def debug_report(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    client = payload.get("client") if isinstance(payload.get("client"), dict) else None
    bundle = await asyncio.to_thread(debug.report, client)
    bundle["text"] = debug.report_text(bundle)
    return web.json_response(bundle)


@routes.post("/chatbot/debug/clear")
async def debug_clear(_request: web.Request) -> web.Response:
    await asyncio.to_thread(debug.clear)
    return web.json_response({"ok": True})


@routes.post("/chatbot/unload")
async def unload_llm(_request: web.Request) -> web.Response:
    config = _effective_config({})
    try:
        result = await providers.unload_models(config)
        debug.log(
            "provider",
            "unload",
            provider=config.get("provider"),
            unloaded=result.get("unloaded"),
            unsupported=result.get("unsupported"),
        )
        return web.json_response(result)
    except Exception as exc:
        debug.log("provider", "unload_error", level="error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=502)


@routes.post("/chatbot/console")
async def console_log(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    config = _effective_config(payload).get("console", {})
    if config.get("enabled") is False:
        return web.json_response({"lines": [], "total": 0, "available": False, "reason": "Console access is disabled in settings."})
    lines = payload.get("lines")
    if lines is None:
        lines = config.get("lines")
    if lines is None:
        lines = 0
    level = payload.get("level")
    result = await asyncio.to_thread(console.recent, lines, level)
    return web.json_response(result)


@routes.get("/chatbot/test_workflow")
async def test_workflow(_request: web.Request) -> web.Response:
    image_path = ""
    image_installed = False
    try:
        import folder_paths

        image_path = os.path.join(folder_paths.get_input_directory(), "vision_red.png")
        image_installed = os.path.isfile(image_path)
    except Exception:
        pass
    return web.json_response({"image": "vision_red.png", "image_installed": image_installed, "image_path": image_path})
