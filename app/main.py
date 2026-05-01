"""Document review agent — entrypoint for AgentCore Runtime.

Streaming protocol: NDJSON. Each yielded chunk is one JSON object + "\\n".
Event types: start | text | tool_start | result | error.

Payload:
{
  "file_uri": "s3://... or local path",
  "file_type": "pdf" | "xlsx",
  "task": "review" | "compliance_check" | "summarize"
}
"""

import json
import logging
import os
import re
import sys
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from dotenv import load_dotenv
from strands import Agent, tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.load import load_model
from tools.file_reader import read_input_file as _read
from tools.file_writer import write_revised_file as _write
from tools.kb_search import search_knowledge_base as _kb
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
        suggestions_json: JSON list. PDF items: {page, bbox?, text}. Excel items: {sheet, cell, text}.
    """
    try:
        suggestions = json.loads(suggestions_json)
    except json.JSONDecodeError as e:
        return f"ERROR: suggestions_json is not valid JSON ({e}). Re-emit the call with a valid JSON array."
    if not isinstance(suggestions, list):
        return "ERROR: suggestions_json must decode to a list. Wrap your suggestions in []."
    return _write(file_uri, file_type, suggestions)


SYSTEM_PROMPT = """You are a document review agent. **All natural-language output (annotations, suggestions, reasoning) MUST be in Traditional Chinese (繁體中文)**. JSON keys remain in English.

Workflow:
1. Call **read_input_file**(file_uri, file_type) to load the user's PDF or Excel file.
2. Identify segments that need verification, improvement, or compliance checking.
3. For each segment, decide which tool to call:
   - **search_knowledge_base**(query) — 查內部政策、SOP、過去審查案件（優先使用）
   - **web_search**(query) — 查外部最新資訊（KB 無相關時才用）
4. Compose a list of suggestions. Each suggestion is a dict with EXACTLY these fields:
   For PDF:
     - page: int (1-indexed)
     - bbox: [x0, y0, x1, y1] in pdf-points-top-left coordinates from read_input_file (omit if location uncertain)
     - text: str — the annotation body shown on the sticky note. Compose in Traditional Chinese as:
         「【嚴重度】原文摘錄：<原文>\\n建議：<建議內容>\\n依據：<KB 案件名稱或網址>」
     - severity: "info" | "warning" | "critical" (used inside text only; not a separate column)
   For Excel:
     - sheet: str
     - cell: str (e.g. 'A1')
     - text: str (same Traditional Chinese format as above)
5. Call **write_revised_file**(file_uri, file_type, suggestions_json) ONCE with the full suggestions list as a JSON string.
6. End your response with a fenced ```json code block (and nothing after it) containing
   exactly this shape (繁體中文內容):
   ```json
   {"status": "ok", "suggestions": [...同上 schema...], "revised_file_uri": "..."}
   ```
   This is mandatory — the orchestrator parses this fenced block as the final result.

Rules:
- Never output Simplified Chinese or English in annotation text. JSON structure stays English.
- Do not invent citations — if no source supports a claim, mark severity "info" in the text
  and explicitly state「無對應依據」.
- Be concise; aim for ≤ 3 sentences per annotation."""


def build_agent() -> Agent:
    """Build a fresh Agent per invocation to avoid carrying conversation history
    between unrelated documents."""
    return Agent(
        model=load_model(),
        system_prompt=SYSTEM_PROMPT,
        tools=[read_input_file, search_knowledge_base, web_search, write_revised_file],
    )


def _ev(type_: str, **fields: Any) -> str:
    return json.dumps({"type": type_, **fields}, ensure_ascii=False) + "\n"


def _build_prompt(file_uri: str, file_type: str, task: str) -> str:
    return (
        f"Task: {task}\n"
        f"file_uri: {file_uri}\n"
        f"file_type: {file_type}\n"
        "Run the workflow and return the final JSON in a fenced ```json block."
    )


_FENCE_OPEN_RE = re.compile(r"```json\s*", re.IGNORECASE)


def _extract_result(final_text: str) -> dict | None:
    """Pull the last ```json fenced block out of the assistant's final text.

    Uses JSONDecoder.raw_decode so nested objects/arrays parse correctly
    (a regex with ``\\{.*?\\}`` would stop at the first inner ``}``).
    """
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
    file_uri = payload.get("file_uri")
    file_type = payload.get("file_type")
    task = payload.get("task", "review")
    if not file_uri or not file_type:
        yield _ev("error", message="file_uri and file_type are required")
        return

    yield _ev("start", file_uri=file_uri, file_type=file_type, task=task)

    seen_tools: set[str] = set()
    final_text_parts: list[str] = []
    last_message: dict | None = None

    try:
        agent = build_agent()
        async for event in agent.stream_async(_build_prompt(file_uri, file_type, task)):
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
        yield _ev("result", data=result)
    except Exception as e:
        log.exception("agent error")
        yield _ev("error", message=str(e))


if __name__ == "__main__":
    app.run()
