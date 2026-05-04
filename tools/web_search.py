"""Tavily web-search wrapper.

Key resolution order:
  1. TAVILY_API_KEY env var (local dev via .env)
  2. AgentCore Identity API Key Credential Provider "tavily-provider"
     (cloud; populated by ./scripts/setup-identity.sh, fetched via
     @requires_api_key at agent cold start — see app/main.py)
"""

import os
from typing import Any, Dict, List

from bedrock_agentcore.identity.auth import requires_api_key
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

# Populated either by env var on first use, or by prewarm_key() at cold start.
_key_cache: Dict[str, str] = {}
_client: TavilyClient | None = None


@requires_api_key(provider_name="tavily-provider")
async def _fetch_from_identity(*, api_key: str) -> None:
    """Pulled from AgentCore Identity (backed by Secrets Manager, agent-aware)."""
    _key_cache["key"] = api_key


async def prewarm_key() -> None:
    """Call once at cold start. env var wins; otherwise hit AgentCore Identity."""
    if "key" in _key_cache:
        return
    env_key = os.getenv("TAVILY_API_KEY")
    if env_key:
        _key_cache["key"] = env_key
        return
    await _fetch_from_identity()


def web_search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    global _client
    if "key" not in _key_cache:
        # Sync fallback for env-var path (local dev where prewarm wasn't awaited).
        env_key = os.getenv("TAVILY_API_KEY")
        if not env_key:
            raise RuntimeError(
                "Tavily key unavailable: set TAVILY_API_KEY for local dev, "
                "or ensure prewarm_key() ran (cloud path via AgentCore Identity)."
            )
        _key_cache["key"] = env_key
    if _client is None:
        _client = TavilyClient(api_key=_key_cache["key"])
    resp = _client.search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")}
        for r in resp.get("results", [])
    ]
