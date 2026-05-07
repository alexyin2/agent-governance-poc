"""Strands HookProvider that writes one memory event per agent invocation.

This is the "Block A" piece of the memory feature: the bare-minimum write
automation that both short-term and long-term reads depend on.

After every invocation the hook writes a single event containing the original
user instruction and the agent's final assistant text. AWS-side strategies
(USER_PREFERENCE / SUMMARY) consume that event asynchronously to populate
long-term memory; the same event is also what get_last_k_turns() returns
for short-term recall.

Tool calls and tool results are intentionally excluded from the saved event
— they bloat the record without adding signal for either strategy.
"""

from __future__ import annotations

import logging
from typing import Any

from bedrock_agentcore.memory import MemoryClient
from strands.hooks import AfterInvocationEvent, HookProvider, HookRegistry

log = logging.getLogger(__name__)


def _extract_text(message: dict[str, Any]) -> str:
    """Pull conversational text from a Strands message; ignore tool blocks."""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        # Skip toolUse / toolResult — only pure text counts as conversation.
        if "toolResult" in block or "toolUse" in block:
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _first_user_text(messages: list[dict[str, Any]]) -> str | None:
    """First user-role message that has actual text (not a tool result)."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = _extract_text(msg)
        if text:
            return text
    return None


def _last_assistant_text(messages: list[dict[str, Any]]) -> str | None:
    """Latest assistant-role message that has actual text."""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        text = _extract_text(msg)
        if text:
            return text
    return None


class DocReviewMemoryHooks(HookProvider):
    """Persist one event per invocation under (actor_id, session_id).

    The event becomes:
      - short-term: readable via get_last_k_turns(session_id) right away
      - long-term:  fed into USER_PREFERENCE / SUMMARY strategies (background)
    """

    def __init__(
        self,
        *,
        memory_id: str,
        actor_id: str,
        session_id: str,
        region: str,
    ) -> None:
        self.memory_id = memory_id
        self.actor_id = actor_id
        self.session_id = session_id
        self.client = MemoryClient(region_name=region)

    def save_event(self, event: AfterInvocationEvent) -> None:
        try:
            messages = getattr(event.agent, "messages", []) or []
        except Exception as e:
            log.warning("memory hook: cannot read agent.messages: %s", e)
            return

        user_text = _first_user_text(messages)
        assistant_text = _last_assistant_text(messages)
        if not user_text or not assistant_text:
            log.info(
                "memory hook: skipping event (user_text=%s, assistant_text=%s)",
                bool(user_text), bool(assistant_text),
            )
            return

        try:
            result = self.client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[
                    (user_text, "USER"),
                    (assistant_text, "ASSISTANT"),
                ],
            )
            log.info(
                "memory hook: saved event id=%s actor=%s session=%s",
                result.get("eventId", "?"), self.actor_id, self.session_id,
            )
        except Exception as e:
            # Never fail the agent because of memory write errors.
            log.exception("memory hook: create_event failed: %s", e)

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterInvocationEvent, self.save_event)
