"""Tavily web-search wrapper.

Key resolution order:
  1. TAVILY_API_KEY env var (local dev via .env)
  2. AgentCore Identity API Key Credential Provider "tavily-provider" (cloud)
"""

import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

_client: TavilyClient | None = None
_cached_key: str | None = None


async def _ensure_key() -> str:
    global _cached_key
    if _cached_key:
        return _cached_key

    env_key = os.getenv("TAVILY_API_KEY")
    if env_key:
        _cached_key = env_key
        return _cached_key

    from bedrock_agentcore.identity.auth import requires_api_key

    holder: dict = {}

    @requires_api_key(provider_name="tavily-provider")
    async def _fetch(*, api_key: str) -> None:
        holder["key"] = api_key

    await _fetch()
    _cached_key = holder["key"]
    return _cached_key


async def web_search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    global _client
    if _client is None:
        _client = TavilyClient(api_key=await _ensure_key())
    resp = _client.search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")}
        for r in resp.get("results", [])
    ]
