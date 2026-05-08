"""Short-term recall: surface earlier turns of the same session into the prompt.

Block C of the memory feature. When a caller carries a previously-issued
session_id back into a new invocation (typical for the feedback flow:
  review  → returns session_id rv-001
  ↓ user reviews findings
  feedback (carries rv-001) → "f2 rejected because ..."
)
the runtime needs to surface the original review's content into the prompt
so the agent knows what `f2` refers to. AgentCore Memory stores the
write-side via the AfterInvocationEvent hook (Block A); this module is the
read-side complement.

If MEMORY_ID is unset, or the session has no recorded events, every helper
here returns an empty list so the agent runs unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from bedrock_agentcore.memory import MemoryClient

log = logging.getLogger(__name__)


def _client() -> MemoryClient | None:
    if not os.getenv("MEMORY_ID"):
        return None
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2"
    return MemoryClient(region_name=region)


def _content_text(content: Any) -> str:
    """Pull plain text out of a stored message's content payload.

    AgentCore returns content in a few shapes — sometimes a string, sometimes
    a {"text": "..."} dict, sometimes a list of blocks. Be permissive and
    extract whatever readable text we can; toolUse / toolResult blocks are
    skipped since they aren't conversational.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if "toolUse" in block or "toolResult" in block:
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        return "\n".join(parts)
    return ""


def load_recent_turns(
    actor_id: str,
    session_id: str,
    k: int = 5,
) -> list[tuple[str, str]]:
    """Return up to `k` most recent turn-pairs flattened into (role, text) items.

    The MemoryClient API returns a list of turns, each turn a list of
    messages. We flatten and strip non-text payloads so the caller can
    drop the result straight into a prompt.

    Returns [] (never raises) when MEMORY_ID is unset, the session has no
    events, or any retrieval error occurs — short-term memory must never
    block the live request.
    """
    if not actor_id or not session_id:
        return []
    client = _client()
    memory_id = os.getenv("MEMORY_ID")
    if client is None or not memory_id:
        return []

    try:
        turns = client.get_last_k_turns(
            memory_id=memory_id,
            actor_id=actor_id,
            session_id=session_id,
            k=k,
        )
    except Exception as e:
        log.warning(
            "load_recent_turns: get_last_k_turns failed (%s); continuing without history",
            e,
        )
        return []

    flat: list[tuple[str, str]] = []
    for turn in turns or []:
        if not isinstance(turn, list):
            continue
        for msg in turn:
            if not isinstance(msg, dict):
                continue
            role = (msg.get("role") or "").upper()
            if role not in ("USER", "ASSISTANT"):
                continue
            text = _content_text(msg.get("content"))
            text = text.strip()
            if text:
                flat.append((role, text))
    return flat


def format_recent_turns_block(turns: list[tuple[str, str]], max_chars: int = 4000) -> str:
    """Render a turn list as a Traditional-Chinese prompt fragment.

    Lightweight cap (max_chars) prevents an extremely long history from
    pushing real instructions out of the context window. We keep the most
    recent turns and trim from the front if needed.
    """
    if not turns:
        return ""
    lines: list[str] = []
    role_label = {"USER": "使用者", "ASSISTANT": "你（先前的回覆）"}
    for role, text in turns:
        label = role_label.get(role, role)
        # Strip very long earlier assistant turns (e.g. JSON dumps); keep enough
        # signal to recognise findings without re-sending entire payloads.
        if len(text) > 800:
            text = text[:800] + "…（已截斷）"
        lines.append(f"[{label}]\n{text}")
    block = "\n\n".join(lines)
    if len(block) > max_chars:
        # Trim from the start so the most recent context survives.
        block = "…（更早的對話已省略）\n\n" + block[-max_chars:]
    return block
