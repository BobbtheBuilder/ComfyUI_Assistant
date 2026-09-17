from __future__ import annotations

from typing import Any, Mapping

import aiohttp

try:
    from .providers import _timeout
except ImportError:
    from providers import _timeout

DEFAULT_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "serpapi": "https://serpapi.com/search.json",
}


async def _read_error(response: aiohttp.ClientResponse) -> str:
    detail = await response.text()
    return f"Search provider returned HTTP {response.status}: {detail}"


async def _tavily(session: aiohttp.ClientSession, key: str, query: str, count: int) -> list[dict[str, str]]:
    body = {"api_key": key, "query": query, "max_results": count, "search_depth": "basic"}
    async with session.post(DEFAULT_ENDPOINTS["tavily"], json=body, timeout=_timeout()) as response:
        if response.status >= 400:
            raise RuntimeError(await _read_error(response))
        payload = await response.json()
    return [
        {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("content", "")}
        for item in payload.get("results", [])
        if isinstance(item, Mapping)
    ]


async def _brave(session: aiohttp.ClientSession, key: str, query: str, count: int) -> list[dict[str, str]]:
    params = {"q": query, "count": count}
    headers = {"Accept": "application/json", "X-Subscription-Token": key}
    async with session.get(
        DEFAULT_ENDPOINTS["brave"], params=params, headers=headers, timeout=_timeout()
    ) as response:
        if response.status >= 400:
            raise RuntimeError(await _read_error(response))
        payload = await response.json()
    results = payload.get("web", {}).get("results", [])
    return [
        {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("description", "")}
        for item in results
        if isinstance(item, Mapping)
    ]


async def _serpapi(session: aiohttp.ClientSession, key: str, query: str, count: int) -> list[dict[str, str]]:
    params = {"q": query, "num": count, "engine": "google", "api_key": key}
    async with session.get(DEFAULT_ENDPOINTS["serpapi"], params=params, timeout=_timeout()) as response:
        if response.status >= 400:
            raise RuntimeError(await _read_error(response))
        payload = await response.json()
    return [
        {"title": item.get("title", ""), "url": item.get("link", ""), "snippet": item.get("snippet", "")}
        for item in payload.get("organic_results", [])
        if isinstance(item, Mapping)
    ]


_ENGINES = {"tavily": _tavily, "brave": _brave, "serpapi": _serpapi}


async def search(config: Mapping[str, Any], query: str, count: int | None = None) -> list[dict[str, str]]:
    websearch = config.get("websearch", {}) if isinstance(config.get("websearch"), Mapping) else {}
    provider = str(websearch.get("provider") or "tavily").lower()
    key = str(websearch.get("api_key") or "").strip()
    engine = _ENGINES.get(provider)
    if engine is None:
        raise RuntimeError(f"Unknown web search provider: {provider}")
    if not key:
        raise RuntimeError("No web search API key configured. Open settings and add one.")
    limit = int(count or websearch.get("max_results") or 5)
    if limit < 1:
        limit = 5
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        return await engine(session, key, query, limit)
