from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Mapping

from aiohttp import web
from server import PromptServer

from . import console, debug, enrich, kb, knowledge, memory, providers, websearch
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
    try:
        known = await asyncio.to_thread(knowledge.status)
    except Exception as exc:
        known = {"error": str(exc)}
    try:
        enrichment = await asyncio.to_thread(enrich.status)
    except Exception as exc:
        enrichment = {"error": str(exc)}
    return web.json_response({"status": kb.status(), "knowledge": known, "enrich": enrichment})


@routes.post("/chatbot/kb/enrich")
async def kb_enrich(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        payload = {}
    limit = int(payload.get("limit") or 5)
    web = payload.get("web") is not False
    retried = 0
    if payload.get("retry", True) is not False:
        try:
            retried = await asyncio.to_thread(enrich.requeue_skipped)
        except Exception:
            retried = 0
    enrich.start_background(limit=limit, web=web)
    return web.json_response({"status": kb.status(), "enrich": enrich.status(), "retried": retried})


@routes.post("/chatbot/kb/research")
async def kb_research(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    entity = str(payload.get("entity") or payload.get("pack") or "").strip()
    if not entity:
        return web.json_response({"error": "Missing entity."}, status=400)
    if not entity.startswith(("pack:", "node:")):
        entity = f"pack:{entity}"
    web = payload.get("web") is not False
    try:
        result = await asyncio.to_thread(enrich.research_entity, entity, web)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


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
    kb.start_background(auto_official=auto_official, refresh_days=refresh_days, force_official=True)
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
    pack = str(payload.get("pack") or "").strip() or None
    mode = str(payload.get("mode") or "").strip()
    limit = int(payload.get("limit") or 6)
    try:
        if mode:
            results = await asyncio.to_thread(kb.retrieve, mode, query, limit, source, pack)
        else:
            results = await asyncio.to_thread(kb.search, query, limit, source, pack)
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
        result = await asyncio.to_thread(kb.node_docs, node_type, int(payload.get("limit") or 8))
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


@routes.post("/chatbot/kb/resolve")
async def kb_resolve(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    text = str(payload.get("text") or payload.get("query") or "").strip()
    if not text:
        return web.json_response({"error": "Missing text."}, status=400)
    try:
        entity = await asyncio.to_thread(knowledge.resolve, text)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"resolved": bool(entity), "entity": entity, "query": text})


@routes.post("/chatbot/kb/compatible")
async def kb_compatible(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    model = str(payload.get("model") or payload.get("text") or "").strip()
    if not model:
        return web.json_response({"error": "Missing model."}, status=400)
    task = str(payload.get("task") or "").strip()
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else None
    try:
        entity = await asyncio.to_thread(knowledge.resolve, model)
        if not entity:
            return web.json_response({"resolved": False, "query": model,
                                      "reason": "Unknown target. Report unresolved; do not substitute."})
        result = await asyncio.to_thread(knowledge.compatible_nodes, entity, task, nodes)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"resolved": True, **result})


@routes.post("/chatbot/kb/context")
async def kb_context(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    model = str(payload.get("model") or payload.get("text") or "").strip()
    if not model:
        return web.json_response({"error": "Missing model."}, status=400)
    task = str(payload.get("task") or "").strip()
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else None
    try:
        result = await asyncio.to_thread(knowledge.get_build_context, model, task, nodes)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


@routes.post("/chatbot/experience")
async def experience(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else None
    try:
        result = await asyncio.to_thread(
            knowledge.record_experience,
            str(payload.get("outcome") or ""),
            str(payload.get("model") or ""),
            str(payload.get("task") or ""),
            str(payload.get("error") or payload.get("error_sig") or ""),
            nodes,
            str(payload.get("pattern_id") or ""),
            "runtime",
            str(payload.get("config_hash") or ""),
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


@routes.post("/chatbot/experience/recall")
async def experience_recall(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    signature = str(payload.get("error") or payload.get("error_sig") or "")
    try:
        matches = await asyncio.to_thread(knowledge.recall_errors, signature, int(payload.get("limit") or 5))
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"matches": matches})


@routes.post("/chatbot/kb/approve")
async def kb_approve(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    pattern_id = str(payload.get("pattern_id") or "").strip()
    if not pattern_id:
        return web.json_response({"error": "Missing pattern_id."}, status=400)
    try:
        result = await asyncio.to_thread(knowledge.approve_pattern, pattern_id)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


@routes.post("/chatbot/kb/evidence")
async def kb_evidence(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    entity = str(payload.get("entity") or "").strip()
    claims = payload.get("claims") if isinstance(payload.get("claims"), list) else []
    if not entity or not claims:
        return web.json_response({"error": "Missing entity or claims."}, status=400)
    try:
        result = await asyncio.to_thread(knowledge.add_claims, entity, claims,
                                         str(payload.get("source") or ""),
                                         str(payload.get("trust") or "WEB_UNVERIFIED"))
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


@routes.get("/chatbot/memory")
async def memory_list(_request: web.Request) -> web.Response:
    lessons = await asyncio.to_thread(memory.list_lessons)
    return web.json_response({"lessons": lessons})


@routes.post("/chatbot/memory")
async def memory_update(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    action = str(payload.get("action") or "add")
    try:
        if action == "add":
            result = await asyncio.to_thread(
                memory.add_lesson,
                str(payload.get("text") or ""),
                str(payload.get("tags") or ""),
                str(payload.get("source") or "user"),
                bool(payload.get("pinned")),
            )
        elif action == "update":
            result = {
                "ok": await asyncio.to_thread(
                    memory.update_lesson,
                    int(payload["id"]),
                    payload.get("text"),
                    payload.get("tags"),
                    payload.get("enabled"),
                    payload.get("pinned"),
                )
            }
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
    lessons = await asyncio.to_thread(
        memory.search, str(payload.get("query") or ""), int(payload.get("limit") or 8)
    )
    return web.json_response({"lessons": lessons})


@routes.post("/chatbot/memory/relevant")
async def memory_relevant(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    lessons = await asyncio.to_thread(
        memory.relevant, str(payload.get("query") or ""), int(payload.get("limit") or 8)
    )
    return web.json_response({"lessons": lessons})


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


@routes.post("/chatbot/debug/event")
async def debug_event(request: web.Request) -> web.Response:
    try:
        payload = await _json_object(request)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    category = str(payload.get("category") or "client")[:40]
    event = str(payload.get("event") or "")[:60]
    level = str(payload.get("level") or "info")[:10]
    raw = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data: dict[str, Any] = {}
    for key, value in list(raw.items())[:20]:
        name = str(key)[:40]
        if name in ("category", "event", "level"):
            continue
        data[name] = value if isinstance(value, (int, float, bool)) else str(value)[:300]
    debug.log(category, event, level=level, **data)
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
    lines = payload.get("lines") or config.get("lines") or 500
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
