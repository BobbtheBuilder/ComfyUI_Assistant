from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from aiohttp import web
from server import PromptServer

from . import console, debug, installer, kb, memory, providers, websearch
from .config_store import CONFIG_STORE

routes = PromptServer.instance.routes


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
        payload = await request.json()
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
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    key = str(payload.get("key") or "__default__")
    CONFIG_STORE.set_history(key, payload.get("messages", []))
    return web.json_response({"ok": True})


@routes.post("/chatbot/models")
async def list_models(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}
    try:
        models = await providers.list_models(_effective_config(payload))
        return web.json_response({"models": models})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


@routes.post("/chatbot/vision")
async def vision_support(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
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
        payload = await request.json()
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
        payload = await request.json()
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
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    messages = payload.get("messages", [])
    tools = payload.get("tools", [])
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
        payload = await request.json()
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


@routes.post("/chatbot/install_git")
async def install_git(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    url = str(payload.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "Missing repository URL."}, status=400)
    result = await asyncio.to_thread(installer.git_install, url, payload.get("name"), payload.get("run_pip", True))
    debug.log(
        "install",
        "git_install.done",
        level="info" if result.get("ok") else "error",
        url=url,
        name=payload.get("name"),
        ok=bool(result.get("ok")),
        pip_ok=result.get("pip_ok"),
        error=result.get("error"),
    )
    return web.json_response(result, status=200 if result.get("ok") else 400)


@routes.get("/chatbot/kb/status")
async def kb_status(_request: web.Request) -> web.Response:
    return web.json_response({"status": kb.status()})


@routes.post("/chatbot/kb/rebuild")
async def kb_rebuild(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
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
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}
    config = _effective_config(payload).get("kb", {})
    refresh_days = int(payload.get("refresh_days", config.get("refresh_days", 7)))
    kb.start_background(auto_official=True, refresh_days=refresh_days, force_official=True)
    return web.json_response({"status": kb.status()})


@routes.post("/chatbot/docs/search")
async def docs_search(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    query = str(payload.get("query") or "").strip()
    if not query:
        return web.json_response({"error": "Missing query."}, status=400)
    source = payload.get("source") or None
    if source not in (None, "pack", "official", "node"):
        source = None
    try:
        results = await asyncio.to_thread(kb.search, query, int(payload.get("limit") or 6), source)
        return web.json_response({"results": results})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


@routes.post("/chatbot/docs/node")
async def docs_node(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
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


@routes.get("/chatbot/memory")
async def memory_list(_request: web.Request) -> web.Response:
    lessons = await asyncio.to_thread(memory.list_lessons)
    return web.json_response({"lessons": lessons})


@routes.post("/chatbot/memory")
async def memory_update(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
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
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    lessons = await asyncio.to_thread(
        memory.search, str(payload.get("query") or ""), int(payload.get("limit") or 8)
    )
    return web.json_response({"lessons": lessons})


@routes.post("/chatbot/memory/relevant")
async def memory_relevant(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body."}, status=400)
    lessons = await asyncio.to_thread(
        memory.relevant, str(payload.get("query") or ""), int(payload.get("limit") or 8)
    )
    return web.json_response({"lessons": lessons})


@routes.post("/chatbot/debug/report")
async def debug_report(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
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
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}
    config = _effective_config(payload).get("console", {})
    if config.get("enabled") is False:
        return web.json_response({"lines": [], "total": 0, "available": False, "reason": "Console access is disabled in settings."})
    lines = payload.get("lines") or config.get("lines") or 500
    level = payload.get("level")
    result = await asyncio.to_thread(console.recent, lines, level)
    return web.json_response(result)
