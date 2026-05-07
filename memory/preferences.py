"""Read-side helpers for AgentCore Memory long-term retrieval.

Block B of the memory feature: lets main.py inject the current actor's
USER_PREFERENCE records into the system prompt at build_agent() time.
The agent itself stays unaware of memory — it just sees a richer prompt.

If MEMORY_ID is not configured (Phase 1 fallback), every helper here
returns an empty list / None so the agent runs unchanged.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from bedrock_agentcore.memory import MemoryClient

log = logging.getLogger(__name__)


PREFERENCES_NAMESPACE_TEMPLATE = "/doc-review/users/{actorId}/preferences"


@lru_cache(maxsize=1)
def _client() -> MemoryClient | None:
    """Module-level singleton — re-using a client across invocations is safe
    and cheaper than constructing one per call."""
    if not os.getenv("MEMORY_ID"):
        return None
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2"
    return MemoryClient(region_name=region)


def _extract_text(record: dict) -> str | None:
    """Pull the text payload out of a retrieve_memories record."""
    content = record.get("content") or {}
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def load_user_preferences(
    actor_id: str,
    query: str = "reviewer preferences",
    top_k: int = 10,
) -> list[str]:
    """Return up to `top_k` preference texts for `actor_id`, or [] if none.

    Returns [] (not raises) when:
      - MEMORY_ID is unset
      - the namespace has no records yet (e.g. brand-new user)
      - retrieval fails for any reason — we never want missing memory to
        block an actual review
    """
    if not actor_id:
        return []
    client = _client()
    memory_id = os.getenv("MEMORY_ID")
    if client is None or not memory_id:
        return []

    namespace = PREFERENCES_NAMESPACE_TEMPLATE.replace("{actorId}", actor_id)
    try:
        records = client.retrieve_memories(
            memory_id=memory_id,
            namespace=namespace,
            query=query,
            top_k=top_k,
        )
    except Exception as e:
        log.warning("load_user_preferences: retrieve failed (%s); continuing without prefs", e)
        return []

    prefs: list[str] = []
    for r in records or []:
        text = _extract_text(r)
        if text:
            prefs.append(text)
    return prefs


def format_preferences_block(prefs: list[str]) -> str:
    """Render a preferences list as a Traditional-Chinese prompt fragment.

    Returns a placeholder line when there are no preferences yet, so the
    system prompt always has *something* in the <preferences> slot
    (helps the LLM ignore it cleanly rather than wonder why it's empty).
    """
    if not prefs:
        return "（此使用者目前尚無記錄的偏好）"
    return "\n".join(f"- {p}" for p in prefs)
