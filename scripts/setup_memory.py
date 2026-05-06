"""One-time setup for the document-review agent's AgentCore Memory resource.

Creates a Memory with two built-in strategies:
  - USER_PREFERENCE: per-user審查偏好 (severity threshold, ignored rule classes, etc.)
  - SUMMARY:        per-review摘要 (document type, main findings, accepted/rejected)

This script is meant to be run **once** during setup. It is idempotent:
  - If a memory with the same name already exists, it is reused.
  - If a strategy already exists on that memory, it is skipped.

Usage:
    # Create (or reuse) the memory + strategies, then print MEMORY_ID
    python scripts/setup_memory.py

    # Show current state without changing anything
    python scripts/setup_memory.py --inspect

    # Force delete & recreate (DESTRUCTIVE — wipes all stored events)
    python scripts/setup_memory.py --force-delete

After a successful create, copy the printed MEMORY_ID into your .env:
    MEMORY_ID=<the printed id>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

from bedrock_agentcore.memory import MemoryClient
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("setup_memory")


# ---------- configuration ----------------------------------------------------

MEMORY_NAME = "agent_governance_poc_memory"
MEMORY_DESCRIPTION = "Document review agent — per-user preferences and per-review summaries"
EVENT_EXPIRY_DAYS = 90  # raw events kept this long; long-term records persist longer

# Built-in strategy definitions. Namespaces use {actorId} / {sessionId} placeholders
# that AWS substitutes when events are written / queries are made.
STRATEGIES: list[dict[str, Any]] = [
    {
        "kind": "user_preference",
        "name": "doc_review_preferences",
        "description": "Reviewer preferences: severity thresholds, ignored rule classes, tone, terminology",
        "namespaces": ["/doc-review/users/{actorId}/preferences"],
    },
    {
        "kind": "summary",
        "name": "doc_review_summaries",
        "description": "Per-review summary: document type, key findings, accepted/rejected decisions",
        "namespaces": ["/doc-review/users/{actorId}/reviews/{sessionId}"],
    },
]


# ---------- helpers ----------------------------------------------------------


def _resolve_region() -> str:
    """Pick region from env, falling back to us-west-2 (matches .bedrock_agentcore.yaml)."""
    return (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _find_existing_memory(client: MemoryClient, name: str) -> dict[str, Any] | None:
    """Return existing memory whose id starts with the configured name, or None.

    AgentCore memory ids look like '<name>-<random>'. The list_memories response
    may not include the name field, so we match on id prefix (this is the same
    approach the SRE agent sample uses).
    """
    try:
        memories = client.list_memories(max_results=100)
    except Exception as e:
        log.error("list_memories failed: %s", e)
        raise

    for m in memories:
        mid = m.get("id", "")
        if mid.startswith(f"{name}-") or m.get("name") == name:
            return m
    return None


def _strategy_status(strategies: list[dict[str, Any]], name: str) -> str | None:
    for s in strategies:
        if s.get("name") == name:
            return s.get("status", "UNKNOWN")
    return None


def _add_strategy(client: MemoryClient, memory_id: str, spec: dict[str, Any]) -> None:
    """Dispatch to the right add_*_strategy_and_wait based on spec['kind']."""
    kind = spec["kind"]
    common = dict(
        memory_id=memory_id,
        name=spec["name"],
        description=spec["description"],
        namespaces=spec["namespaces"],
    )
    if kind == "user_preference":
        client.add_user_preference_strategy_and_wait(**common)
    elif kind == "summary":
        client.add_summary_strategy_and_wait(**common)
    elif kind == "semantic":
        client.add_semantic_strategy_and_wait(**common)
    else:
        raise ValueError(f"unknown strategy kind: {kind}")


def _ensure_strategies(client: MemoryClient, memory: dict[str, Any]) -> None:
    """Add any STRATEGIES that aren't already present on this memory."""
    memory_id = memory["id"]
    existing = memory.get("strategies", []) or []
    existing_names = {s.get("name") for s in existing}

    for spec in STRATEGIES:
        if spec["name"] in existing_names:
            status = _strategy_status(existing, spec["name"])
            log.info("strategy already exists: %s (status=%s)", spec["name"], status)
            continue
        log.info("adding strategy: %s (%s)", spec["name"], spec["kind"])
        _add_strategy(client, memory_id, spec)
        log.info("  ✓ %s active", spec["name"])


def _wait_until_active(client: MemoryClient, memory_id: str, timeout_s: int = 180) -> dict[str, Any]:
    """Poll until the memory leaves CREATING and reaches ACTIVE (or fail).

    AgentCore CreateMemory returns immediately with status=CREATING; subsequent
    UpdateMemory calls (AddStrategy) reject the request until the memory is
    ACTIVE. We poll list_memories rather than get_memory because the SDK's
    list_memories returns the status field consistently.
    """
    log.info("waiting for memory %s to become ACTIVE…", memory_id)
    deadline = time.time() + timeout_s
    last_status = None
    while time.time() < deadline:
        for m in client.list_memories(max_results=100):
            if m.get("id") == memory_id:
                status = m.get("status", "UNKNOWN")
                if status != last_status:
                    log.info("  status: %s", status)
                    last_status = status
                if status == "ACTIVE":
                    return m
                if status == "FAILED":
                    raise RuntimeError(f"memory {memory_id} entered FAILED state")
                break
        time.sleep(5)
    raise TimeoutError(
        f"memory {memory_id} did not reach ACTIVE within {timeout_s}s "
        f"(last status: {last_status})"
    )


def _create_memory(client: MemoryClient) -> dict[str, Any]:
    log.info("creating memory: %s", MEMORY_NAME)
    result = client.create_memory(
        name=MEMORY_NAME,
        description=MEMORY_DESCRIPTION,
        event_expiry_days=EVENT_EXPIRY_DAYS,
    )
    log.info("  ✓ memory id: %s (status=%s)", result["id"], result.get("status", "?"))
    # AddMemoryStrategy is rejected while memory is CREATING — block until ACTIVE.
    return _wait_until_active(client, result["id"])


def _delete_memory(client: MemoryClient, memory_id: str) -> None:
    log.warning("deleting memory: %s", memory_id)
    client.delete_memory(memory_id)
    # Service is async — wait so a follow-up create doesn't race the delete.
    time.sleep(5)


# ---------- commands ---------------------------------------------------------


def cmd_inspect(client: MemoryClient) -> int:
    existing = _find_existing_memory(client, MEMORY_NAME)
    if not existing:
        print(f"(no memory found with name prefix '{MEMORY_NAME}')")
        return 1
    print(f"memory_id : {existing['id']}")
    print(f"status    : {existing.get('status', 'UNKNOWN')}")
    strategies = existing.get("strategies", []) or []
    print(f"strategies: {len(strategies)}")
    for s in strategies:
        print(f"  - {s.get('name')} [{s.get('type', '?')}] status={s.get('status')}")
        for ns in s.get("namespaces", []) or []:
            print(f"      namespace: {ns}")
    return 0


def cmd_setup(client: MemoryClient, force_delete: bool) -> int:
    existing = _find_existing_memory(client, MEMORY_NAME)

    if existing and force_delete:
        _delete_memory(client, existing["id"])
        existing = None

    if existing:
        log.info("reusing existing memory: %s", existing["id"])
        memory = existing
    else:
        memory = _create_memory(client)

    _ensure_strategies(client, memory)

    # Re-inspect so the printed id reflects current state
    final = _find_existing_memory(client, MEMORY_NAME) or memory
    print()
    print("=" * 60)
    print(f"  MEMORY_ID = {final['id']}")
    print("=" * 60)
    print()
    print("Add this line to your .env file:")
    print(f"  MEMORY_ID={final['id']}")
    return 0


# ---------- entry ------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--inspect", action="store_true",
                        help="show current state, do not modify anything")
    parser.add_argument("--force-delete", action="store_true",
                        help="DESTRUCTIVE: delete existing memory before recreating")
    args = parser.parse_args()

    region = _resolve_region()
    log.info("region: %s", region)
    client = MemoryClient(region_name=region)

    if args.inspect:
        return cmd_inspect(client)
    return cmd_setup(client, force_delete=args.force_delete)


if __name__ == "__main__":
    sys.exit(main())
