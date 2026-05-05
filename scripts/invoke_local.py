"""Smoke test — invoke the agent and pretty-print the NDJSON event stream.

New payload shape (tool-based agent planning):
    {actor_id, instruction, files?: [{uri, type}], session_id?}

Usage (in another terminal):
    agentcore dev

Then any of:
    # 純諮詢（無檔案）
    python scripts/invoke_local.py "我之前審過幾份 CAB 申請？"

    # 純審查（一份檔案）
    python scripts/invoke_local.py "請審查附檔" samples/input_sample.pdf

    # 混合任務（多份檔案 + 自然語言）
    python scripts/invoke_local.py "審這份新檔並對照過去 CAB" samples/a.pdf samples/b.xlsx

    # 指定 actor_id
    python scripts/invoke_local.py --actor user-123 "請審查" samples/input_sample.pdf
"""

import argparse
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
BOLD = "\033[1m"
RESET = "\033[0m"


def _infer_type(path: str) -> str:
    p = path.lower()
    if p.endswith(".pdf"):
        return "pdf"
    if p.endswith(".xlsx") or p.endswith(".xlsm") or p.endswith(".xls"):
        return "xlsx"
    raise SystemExit(f"cannot infer type from path {path!r}; please rename or pass explicitly")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Invoke the document-review agent locally.")
    p.add_argument("--actor", "-a", default="local-tester", help="actor_id (default: local-tester)")
    p.add_argument("--session", "-s", default=None, help="optional session_id")
    p.add_argument("instruction", help="natural-language instruction (繁體中文 OK)")
    p.add_argument("files", nargs="*", help="zero or more file paths (pdf / xlsx)")
    return p.parse_args()


def main():
    args = _parse_args()
    files = [{"uri": path, "type": _infer_type(path)} for path in args.files]

    payload: dict = {
        "actor_id": args.actor,
        "instruction": args.instruction,
        "files": files,
    }
    if args.session:
        payload["session_id"] = args.session

    print(f"{BOLD}POST {DEV_URL}{RESET}")
    print(f"{DIM}{json.dumps(payload, ensure_ascii=False, indent=2)}{RESET}\n")

    resp = requests.post(DEV_URL, json=payload, stream=True, timeout=600)
    resp.raise_for_status()

    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode(errors="replace")
        # Strip SSE "data: " prefix if present
        if line.startswith("data: "):
            line = line[6:]
            try:
                inner = json.loads(line)
                if isinstance(inner, str):
                    line = inner.rstrip("\n")
            except json.JSONDecodeError:
                pass
        elif line.startswith(":"):
            continue  # SSE keepalive
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            print(f"{DIM}{line}{RESET}")
            continue

        t = ev.get("type")
        if t == "start":
            files_str = ", ".join(f"{f['type']}:{f['uri']}" for f in ev.get("files", [])) or "(no files)"
            print(f"{CYAN}▶ start{RESET} actor={ev.get('actor_id')} session={ev.get('session_id')}")
            print(f"  {DIM}files: {files_str}{RESET}")
            print(f"  {DIM}instruction: {ev.get('instruction')}{RESET}\n")
        elif t == "text":
            sys.stdout.write(ev.get("delta", ""))
            sys.stdout.flush()
        elif t == "tool_start":
            inp = json.dumps(ev.get("input"), ensure_ascii=False)
            if len(inp) > 140:
                inp = inp[:140] + "…"
            print(f"\n{YELLOW}→ tool: {ev.get('name')}{RESET} {DIM}{inp}{RESET}")
        elif t == "result":
            print(f"\n{GREEN}✓ result{RESET}")
            print(json.dumps(ev.get("data"), indent=2, ensure_ascii=False))
        elif t == "error":
            print(f"\n{RED}✗ error: {ev.get('message')}{RESET}")
        else:
            print(f"{DIM}{ev}{RESET}")


if __name__ == "__main__":
    main()
