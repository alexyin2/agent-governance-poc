"""Document review agent — entrypoint for AgentCore Runtime.

Streaming protocol: NDJSON. Each yielded chunk is one JSON object + "\\n".
Event types: start | text | tool_start | result | error.

Payload (tool-based agent planning, no mode dispatch):
{
  "actor_id": "user-123",                                  # required
  "instruction": "請審查附檔，並對照我之前的 CAB 案件",      # required (natural language)
  "files": [                                               # optional
    {"uri": "s3://... or local path", "type": "pdf|xlsx"}
  ],
  "session_id": "rv-..."                                   # optional, agent generates if absent
}
"""

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from dotenv import load_dotenv
from strands import Agent, tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.hooks import DocReviewMemoryHooks
from memory.preferences import format_preferences_block, load_user_preferences
from model.load import load_model
from tools.file_reader import read_input_file as _read
from tools.file_writer import write_revised_file as _write
from tools.kb_search import search_knowledge_base as _kb
from tools.web_search import prewarm_key as _prewarm_tavily_key
from tools.web_search import web_search as _web

load_dotenv()

app = BedrockAgentCoreApp()
log = app.logger
logging.basicConfig(level=logging.INFO)


MAX_PDF_PAGES = 40
MAX_XLSX_CELLS_PER_SHEET = 500


def _truncate_for_llm(content: dict) -> dict:
    """Structurally trim oversized content so the LLM gets valid JSON, not a sliced string."""
    if content.get("type") == "pdf":
        pages = content.get("pages", [])
        if len(pages) > MAX_PDF_PAGES:
            content = {**content, "pages": pages[:MAX_PDF_PAGES],
                       "truncated": f"showing first {MAX_PDF_PAGES} of {len(pages)} pages"}
    elif content.get("type") == "xlsx":
        sheets = []
        truncated = False
        for sh in content.get("sheets", []):
            cells = sh.get("cells", [])
            if len(cells) > MAX_XLSX_CELLS_PER_SHEET:
                truncated = True
                sheets.append({**sh, "cells": cells[:MAX_XLSX_CELLS_PER_SHEET],
                               "truncated": f"showing first {MAX_XLSX_CELLS_PER_SHEET} of {len(cells)} cells"})
            else:
                sheets.append(sh)
        if truncated:
            content = {**content, "sheets": sheets}
    return content


@tool
def read_input_file(file_uri: str, file_type: str) -> str:
    """Read a PDF or Excel file and return its structured contents as JSON.

    PDF coordinates use the 'pdf-points-top-left' system (origin top-left, units in
    PDF points). Each page reports its width/height. When you later call
    write_revised_file with a bbox, use the same coordinate system.

    Args:
        file_uri: Local path or s3:// URI of the input file.
        file_type: 'pdf' or 'xlsx'.
    """
    return json.dumps(_truncate_for_llm(_read(file_uri, file_type)))


@tool
def search_knowledge_base(query: str) -> str:
    """Retrieve relevant passages from the enterprise Bedrock Knowledge Base.

    Args:
        query: A natural-language query about company policy, SOP, or reference material.
    """
    return json.dumps(_kb(query))


@tool
def web_search(query: str) -> str:
    """Search the public web (Tavily) for up-to-date information not in the KB.

    Args:
        query: A natural-language web search query.
    """
    return json.dumps(_web(query))


@tool
def write_revised_file(file_uri: str, file_type: str, suggestions_json: str) -> str:
    """Write suggestions back into the source file as annotations (PDF) or comments (Excel).

    Args:
        file_uri: Original file location (same as read_input_file input).
        file_type: 'pdf' or 'xlsx'.
        suggestions_json: JSON list. PDF items: {finding_id, page, bbox?, text, severity}.
                          Excel items: {finding_id, sheet, cell, text, severity}.
    """
    try:
        suggestions = json.loads(suggestions_json)
    except json.JSONDecodeError as e:
        return f"ERROR: suggestions_json is not valid JSON ({e}). Re-emit the call with a valid JSON array."
    if not isinstance(suggestions, list):
        return "ERROR: suggestions_json must decode to a list. Wrap your suggestions in []."
    return _write(file_uri, file_type, suggestions)


SYSTEM_PROMPT = """你是文件審查與顧問助理。所有自然語言輸出（包含註記、建議、推理、回答）**必須使用繁體中文**；JSON keys 維持英文。

## 你的能力
1. **審查文件** — 當使用者提供 files 時，讀取、分析並把建議寫回檔案。
2. **一般諮詢** — 回答跟過去審查、政策、法規相關的問題（無需檔案）。
3. **混合任務** — 同時執行上述兩者（例如：審這份新檔案並對照過去案件）。

## 使用者偏好（由系統從過去互動中萃取，請納入審查與回答考量）
<preferences>
{PREFERENCES_BLOCK}
</preferences>

當使用者偏好與本次 instruction 衝突時，以本次 instruction 為主，並在 answer 中簡短說明為何忽略偏好。

## 可用工具
- `read_input_file(file_uri, file_type)` — 讀取 PDF / Excel 結構化內容
- `search_knowledge_base(query)` — 查內部政策、SOP、過去案件（優先使用）
- `web_search(query)` — 查公開網路資訊（KB 沒有時才用）
- `write_revised_file(file_uri, file_type, suggestions_json)` — 把建議寫回檔案（PDF 加註記、Excel 加留言）

## 決策原則
- 依照使用者 instruction 判斷意圖，自由組合工具完成任務
- 若使用者要求審查且有 files：每份檔都要 `read_input_file` → 分析 → 必要時查 KB / web → **務必呼叫 `write_revised_file`** 寫回每份有建議的檔案
- 若使用者只是提問（無 files）：直接回答；必要時用 `search_knowledge_base` 補充
- 若有多份檔案，要做跨檔一致性檢查
- PDF 座標使用 `read_input_file` 回傳的 pdf-points-top-left 系統（原點在左上）

## 審查建議格式
每個 suggestion 必須包含 `finding_id`（你自己編，例如 `f1`, `f2`…，每份檔案內唯一）。

PDF suggestion 欄位：
- `finding_id`: str
- `page`: int (1-indexed)
- `bbox`: [x0, y0, x1, y1]（pdf-points-top-left；不確定時可省略）
- `text`: str — 註記內文，繁體中文，格式：
  「【嚴重度】原文摘錄：<原文>\\n建議：<建議內容>\\n依據：<KB 案件名稱或網址>」
- `severity`: "info" | "warning" | "critical"

Excel suggestion 欄位：
- `finding_id`, `sheet`, `cell` (e.g. 'A1'), `text`, `severity`（同上）

## 最終回覆格式（強制）
你的最後一段回覆**必須**以一個 fenced ```json 區塊結尾（後面不可再有任何文字），內容如下：

```json
{
  "status": "ok",
  "session_id": "<從使用者 prompt 取得的 session_id 原樣填回>",
  "answer": "<繁體中文摘要或直接回答；必填>",
  "files": [
    {
      "input_uri": "<原始 file uri>",
      "revised_file_uri": "<write_revised_file 回傳的 uri；無建議則同 input_uri>",
      "suggestions": [ ...該檔的 suggestions array... ]
    }
  ],
  "cross_findings": "<繁體中文段落；無跨檔問題或單檔請填「無跨檔問題」；純諮詢請填「不適用」>"
}
```

若是純諮詢任務（沒有處理檔案），`files` 填 `[]`，`cross_findings` 填「不適用」，回答寫在 `answer`。

## 注意事項
- 不可使用簡體中文或英文撰寫註記內文
- 沒有依據時請註明「無對應依據」，severity 設為 "info"
- 註記精簡，每則 ≤ 3 句
- 不要在最終 ```json 區塊之後再寫任何文字
"""


def _build_memory_hooks(actor_id: str, session_id: str) -> list:
    """Attach the memory write-hook only when MEMORY_ID is configured.

    Without it the agent runs exactly like before (Phase 1 behavior); with it,
    every invocation persists one event under (actor_id, session_id).
    """
    memory_id = os.getenv("MEMORY_ID")
    if not memory_id:
        log.info("MEMORY_ID not set — running without memory persistence")
        return []
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2"
    return [DocReviewMemoryHooks(
        memory_id=memory_id,
        actor_id=actor_id,
        session_id=session_id,
        region=region,
    )]


def _build_system_prompt(actor_id: str) -> str:
    """Inject the actor's stored USER_PREFERENCE records into the prompt.

    Returns SYSTEM_PROMPT with the {PREFERENCES_BLOCK} placeholder replaced
    by either a bullet list of preferences or a placeholder line. Never
    raises — if memory retrieval fails, prefs is [] and the agent runs as
    if this user has no prior preferences.
    """
    prefs = load_user_preferences(actor_id)
    if prefs:
        log.info("injected %d preference(s) for actor=%s", len(prefs), actor_id)
    return SYSTEM_PROMPT.replace("{PREFERENCES_BLOCK}", format_preferences_block(prefs))


def build_agent(actor_id: str, session_id: str) -> Agent:
    """Build a fresh Agent per invocation to avoid carrying conversation history
    between unrelated requests."""
    return Agent(
        model=load_model(),
        system_prompt=_build_system_prompt(actor_id),
        tools=[read_input_file, search_knowledge_base, web_search, write_revised_file],
        hooks=_build_memory_hooks(actor_id, session_id),
    )


def _ev(type_: str, **fields: Any) -> str:
    return json.dumps({"type": type_, **fields}, ensure_ascii=False) + "\n"


def _new_session_id() -> str:
    """Generate a readable session id like rv-20260505T142233-ab12cd."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"rv-{ts}-{suffix}"


def _normalize_files(raw_files: Any) -> tuple[list[dict] | None, str | None]:
    """Validate optional `files` list. Returns (files_or_None, error_or_None)."""
    if raw_files is None:
        return None, None
    if not isinstance(raw_files, list):
        return None, "files must be a list of {uri, type} objects"
    if not raw_files:
        return [], None
    normalized: list[dict] = []
    for i, f in enumerate(raw_files):
        if not isinstance(f, dict):
            return None, f"files[{i}] must be an object"
        uri = f.get("uri")
        ftype = f.get("type")
        if not uri or not ftype:
            return None, f"files[{i}] requires both 'uri' and 'type'"
        if ftype not in ("pdf", "xlsx"):
            return None, f"files[{i}].type must be 'pdf' or 'xlsx', got {ftype!r}"
        normalized.append({"uri": uri, "type": ftype})
    return normalized, None


def _validate_payload(payload: dict) -> tuple[dict | None, str | None]:
    """Strict validation of the new payload shape. No legacy compatibility."""
    if not isinstance(payload, dict):
        return None, "payload must be a JSON object"

    # Reject legacy fields explicitly so old callers fail loudly.
    legacy_fields = [k for k in ("file_uri", "file_type", "task", "mode") if k in payload]
    if legacy_fields:
        return None, (
            f"legacy payload fields not supported: {legacy_fields}. "
            "use {actor_id, instruction, files?} instead."
        )

    actor_id = payload.get("actor_id")
    if not isinstance(actor_id, str) or not actor_id.strip():
        return None, "actor_id is required and must be a non-empty string"

    instruction = payload.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return None, "instruction is required and must be a non-empty string"

    files, err = _normalize_files(payload.get("files"))
    if err:
        return None, err

    session_id = payload.get("session_id") or _new_session_id()
    if not isinstance(session_id, str) or not session_id.strip():
        return None, "session_id, when provided, must be a non-empty string"

    return {
        "actor_id": actor_id.strip(),
        "instruction": instruction.strip(),
        "files": files or [],
        "session_id": session_id,
    }, None


def _build_prompt(req: dict) -> str:
    """Compose the user-turn prompt from the validated request."""
    lines = [
        f"actor_id: {req['actor_id']}",
        f"session_id: {req['session_id']}",
        f"files_count: {len(req['files'])}",
    ]
    for i, f in enumerate(req["files"], 1):
        lines.append(f"  file {i}: uri={f['uri']}  type={f['type']}")
    lines.append("")
    lines.append("使用者指令（請依此 instruction 自行決定要呼叫哪些工具）：")
    lines.append(req["instruction"])
    lines.append("")
    lines.append(f"完成後請以 fenced ```json 區塊回覆，且 session_id 欄位填 {req['session_id']!r}。")
    return "\n".join(lines)


_FENCE_OPEN_RE = re.compile(r"```json\s*", re.IGNORECASE)


def _extract_result(final_text: str) -> dict | None:
    """Pull the last ```json fenced block out of the assistant's final text."""
    if not final_text:
        return None
    decoder = json.JSONDecoder()
    last_obj: dict | None = None
    for m in _FENCE_OPEN_RE.finditer(final_text):
        start = m.end()
        json_start = final_text.find("{", start)
        if json_start == -1:
            continue
        try:
            obj, _end = decoder.raw_decode(final_text, json_start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            last_obj = obj
    return last_obj


def _message_text(message: dict | None) -> str:
    """Extract concatenated text from a Strands message (list of content blocks)."""
    if not message:
        return ""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


@app.entrypoint
async def invoke(payload, context=None):
    log.info(f"Payload: {payload}")
    req, err = _validate_payload(payload or {})
    if err:
        yield _ev("error", message=err)
        return

    yield _ev(
        "start",
        actor_id=req["actor_id"],
        session_id=req["session_id"],
        files=req["files"],
        instruction=req["instruction"],
    )

    # Cold-start prewarm: pull Tavily key from env (local) or AgentCore Identity (cloud).
    try:
        await _prewarm_tavily_key()
    except Exception as e:
        log.warning(f"Tavily key prewarm failed (web_search will error if used): {e}")

    seen_tools: set[str] = set()
    final_text_parts: list[str] = []
    last_message: dict | None = None

    try:
        agent = build_agent(req["actor_id"], req["session_id"])
        async for event in agent.stream_async(_build_prompt(req)):
            data = event.get("data")
            if isinstance(data, str):
                final_text_parts.append(data)
                yield _ev("text", delta=data)

            tu = event.get("current_tool_use")
            if tu:
                tu_id = tu.get("toolUseId")
                if tu_id and tu_id not in seen_tools:
                    seen_tools.add(tu_id)
                    yield _ev("tool_start", name=tu.get("name"), input=tu.get("input"))

            if event.get("message"):
                last_message = event["message"]

        final_text = _message_text(last_message) or "".join(final_text_parts)
        result = _extract_result(final_text)
        if result is None:
            yield _ev("error", message="failed to parse final ```json block")
            return
        # Ensure session_id is present in result (agent may forget despite prompt)
        result.setdefault("session_id", req["session_id"])
        yield _ev("result", data=result)
    except Exception as e:
        log.exception("agent error")
        yield _ev("error", message=str(e))


if __name__ == "__main__":
    app.run()
