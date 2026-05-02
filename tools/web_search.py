"""Tavily web-search wrapper.

Key resolution order:
  1. TAVILY_API_KEY env var (local dev via .env)
  2. AWS Secrets Manager "agent-governance-poc/tavily-api-key" (cloud)
"""

import json
import os
from functools import lru_cache
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

_client: TavilyClient | None = None


@lru_cache(maxsize=1)
def _get_key() -> str:
    env_key = os.getenv("TAVILY_API_KEY")
    if env_key:
        return env_key

    region = os.getenv("AWS_REGION", "us-west-2")
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        resp = sm.get_secret_value(SecretId="agent-governance-poc/tavily-api-key")
        secret = resp["SecretString"]
        try:
            return json.loads(secret)["TAVILY_API_KEY"]
        except (json.JSONDecodeError, KeyError):
            return secret
    except ClientError as e:
        raise RuntimeError(f"Failed to fetch Tavily key from Secrets Manager: {e}") from e


def web_search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    global _client
    if _client is None:
        _client = TavilyClient(api_key=_get_key())
    resp = _client.search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")}
        for r in resp.get("results", [])
    ]
