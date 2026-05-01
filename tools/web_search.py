"""Tavily web-search wrapper."""

import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

_client: TavilyClient | None = None


def _get_client() -> TavilyClient:
    global _client
    if _client is None:
        key = os.getenv("TAVILY_API_KEY")
        if not key:
            raise RuntimeError("TAVILY_API_KEY not set")
        _client = TavilyClient(api_key=key)
    return _client


def web_search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    resp = _get_client().search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")}
        for r in resp.get("results", [])
    ]
