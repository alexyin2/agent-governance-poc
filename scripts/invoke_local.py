"""Smoke test — invoke the agent and pretty-print the NDJSON event stream.

Usage (in another terminal):
    agentcore dev
Then:
    python scripts/invoke_local.py samples/input_sample.pdf
"""

import json
import sys

import requests

DEV_URL = "http://localhost:8080/invocations"

# ANSI colors
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def main():
    if len(sys.argv) < 2:
        print("usage: invoke_local.py <file_path> [pdf|xlsx]")
        sys.exit(1)
    path = sys.argv[1]
    ftype = sys.argv[2] if len(sys.argv) > 2 else ("pdf" if path.endswith(".pdf") else "xlsx")
    payload = {"file_uri": path, "file_type": ftype, "task": "review"}

    resp = requests.post(DEV_URL, json=payload, stream=True, timeout=600)
    resp.raise_for_status()

    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode(errors="replace")
        # Strip SSE "data: " prefix if present
        if line.startswith("data: "):
            line = line[6:]
            # SSE payload may itself be a JSON-encoded string wrapping the NDJSON event
            try:
                inner = json.loads(line)
                if isinstance(inner, str):
                    line = inner.rstrip("\n")
            except json.JSONDecodeError:
                pass
        elif line.startswith(":"):
            continue  # SSE comment / keepalive
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            print(f"{DIM}{line}{RESET}")
            continue

        t = ev.get("type")
        if t == "start":
            print(f"{CYAN}▶ start{RESET} {ev.get('file_uri')} ({ev.get('file_type')}, task={ev.get('task')})")
        elif t == "text":
            sys.stdout.write(ev.get("delta", ""))
            sys.stdout.flush()
        elif t == "tool_start":
            print(f"\n{YELLOW}→ tool: {ev.get('name')}{RESET} {DIM}{json.dumps(ev.get('input'), ensure_ascii=False)[:120]}{RESET}")
        elif t == "result":
            print(f"\n{GREEN}✓ result{RESET}")
            print(json.dumps(ev.get("data"), indent=2, ensure_ascii=False))
        elif t == "error":
            print(f"\n{RED}✗ error: {ev.get('message')}{RESET}")
        else:
            print(f"{DIM}{ev}{RESET}")


if __name__ == "__main__":
    main()
