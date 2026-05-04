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

The user may submit ONE OR MORE files in a single review session. Files can be related (e.g. a CAB application PDF + its risk-assessment Excel) and should be cross-validated against each other.

Workflow:
1. For EACH input file, call **read_input_file**(file_uri, file_type) once. Remember the
   uri and type of every file — you will need them in step 5.
2. After reading every file, identify two kinds of issues:
   (a) per-file issues — segments that need verification, improvement, or compliance
       checking inside one file
   (b) cross-file issues — inconsistencies, gaps, or missing alignment BETWEEN files
       (e.g. PDF says X but Excel checklist marks ✗ for X; Excel claims completeness
       but PDF lacks the corresponding section)
3. For each segment that needs evidence, decide which tool to call:
   - **search_knowledge_base**(query) — 查內部政策、SOP、過去審查案件（優先使用）
   - **web_search**(query) — 查外部最新資訊（KB 無相關時才用）
4. Compose suggestions, separating them by source file. Each suggestion belongs to ONE file.
   PDF suggestion fields:
     - page: int (1-indexed)
     - bbox: [x0, y0, x1, y1] in pdf-points-top-left coordinates from read_input_file (omit if uncertain)
     - text: str — annotation body. Format in Traditional Chinese as:
         「【嚴重度】原文摘錄：<原文>\\n建議：<建議內容>\\n依據：<KB 案件名稱或網址>」
     - severity: "info" | "warning" | "critical" (used inside text only)
   Excel suggestion fields:
     - sheet: str
     - cell: str (e.g. 'A1')
     - text: str (same Traditional Chinese format)
5. For EACH file that has at least one per-file suggestion, call **write_revised_file**(file_uri,
   file_type, suggestions_json) ONCE. Pass only that file's suggestions, not a mixed list.
   If a file has zero suggestions, skip the write call for it.
6. End your response with a fenced ```json code block (and nothing after it) containing exactly:
   ```json
   {
     "status": "ok",
     "files": [
       {
         "input_uri": "<original file_uri>",
         "revised_file_uri": "<URI returned by write_revised_file, or same as input_uri if no suggestions>",
         "suggestions": [...per-file suggestions array...]
       }
     ],
     "cross_findings": "<繁體中文段落，描述跨檔的一致性問題與發現；若無則填「無跨檔問題」>"
   }
   ```
   This is mandatory — the orchestrator parses this fenced block as the final result.

Rules:
- Never output Simplified Chinese or English in annotation text. JSON keys stay English.
- Do not invent citations — if no source supports a claim, mark severity "info" and state「無對應依據」.
- Be concise; aim for ≤ 3 sentences per annotation.
- For single-file submissions, the `files` array just has one entry, and
  `cross_findings` is「無跨檔問題（單一檔案）」."""


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


def _build_prompt(files: list[dict], task: str) -> str:
    """Build the user-turn prompt. `files` is a list of {"uri","type"} dicts."""
    lines = [f"Task: {task}", f"Number of files: {len(files)}"]
    for i, f in enumerate(files, 1):
        lines.append(f"File {i}: uri={f['uri']}  type={f['type']}")
    lines.append("Run the multi-file workflow and return the final JSON in a fenced ```json block.")
    return "\n".join(lines)


def _normalize_files(payload: dict) -> tuple[list[dict] | None, str | None]:
    """Resolve payload to a list of {uri,type} dicts.

    Accepts two payload shapes:
      new:    {"files": [{"uri": "...", "type": "pdf|xlsx"}, ...]}
      legacy: {"file_uri": "...", "file_type": "pdf|xlsx"}

    Returns (files, error). Exactly one of them is None.
    """
    files = payload.get("files")
    if files is not None:
        if not isinstance(files, list) or not files:
            return None, "files must be a non-empty list"
        normalized: list[dict] = []
        for i, f in enumerate(files):
            if not isinstance(f, dict):
                return None, f"files[{i}] must be an object"
            uri = f.get("uri") or f.get("file_uri")
            ftype = f.get("type") or f.get("file_type")
            if not uri or not ftype:
                return None, f"files[{i}] requires uri and type"
            normalized.append({"uri": uri, "type": ftype})
        return normalized, None
    # Legacy single-file shape
    uri = payload.get("file_uri")
    ftype = payload.get("file_type")
    if not uri or not ftype:
        return None, "either `files` list, or `file_uri`+`file_type`, is required"
    return [{"uri": uri, "type": ftype}], None


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
    task = payload.get("task", "review")
    files, err = _normalize_files(payload)
    if err:
        yield _ev("error", message=err)
        return

    yield _ev("start", files=files, task=task)

    # Cold-start prewarm: pull Tavily key from env (local) or AgentCore Identity (cloud).
    # Done here (not at module import) so the runtime workload context is available.
    try:
        await _prewarm_tavily_key()
    except Exception as e:
        log.warning(f"Tavily key prewarm failed (web_search will error if used): {e}")

    seen_tools: set[str] = set()
    final_text_parts: list[str] = []
    last_message: dict | None = None

    try:
        agent = build_agent()
        async for event in agent.stream_async(_build_prompt(files, task)):
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
