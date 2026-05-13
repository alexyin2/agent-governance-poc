"""Document review agent — entrypoint for AgentCore Runtime.

Streaming protocol: NDJSON. Each yielded chunk is one JSON object + "\\n".
Event types: start | text | tool_start | result | error.

Payload:
{
  "actor_id": "user-123",                                  # optional (stateless if absent)
  "instruction": "請審查附檔，並對照我之前的 CAB 案件",      # REQUIRED
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
from pathlib import Path
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from dotenv import load_dotenv
from strands import Agent, tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.hooks import DocReviewMemoryHooks
from memory.preferences import format_preferences_block, load_user_preferences
from memory.short_term import format_recent_turns_block, load_recent_turns
from model.load import load_model
from tools.file_loader import infer_file_type as _infer_type
from tools.file_loader import load_file as _load_file
from tools.file_loader import read_uri_bytes as _read_uri_bytes
from tools.file_writer import annotate_pdf as _annotate_pdf
from tools.file_writer import annotate_xlsx as _annotate_xlsx
from tools.kb_search import search_knowledge_base as _kb
from tools.pdf_inspect import inspect_pdf_page as _inspect_pdf
from tools.web_search import prewarm_key as _prewarm_tavily_key
from tools.web_search import web_search as _web
from tools.xlsx_inspect import inspect_xlsx_sheet as _inspect_xlsx

load_dotenv()

app = BedrockAgentCoreApp()
log = app.logger
logging.basicConfig(level=logging.INFO)


# Path to the externalized system prompt (XML-tagged sections).
_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"


@tool
def load_file(file_uri: str) -> dict:
    """Load a PDF or Excel file into the conversation so you can see it.

    When to call:
    - The user references a file that was not attached in this turn's payload
      (typically a uri found in <recent_history> from an earlier turn).
    - You need to re-examine a previously discussed file at a deeper level.

    Do NOT call this for files already in this turn's payload — those are
    pre-loaded automatically before you start.

    The file type is inferred from the URI extension (.pdf / .xlsx / .xlsm / .xls).

    Args:
        file_uri: An ``s3://bucket/key`` URI or a local filesystem path.
    """
    return _load_file(file_uri)


@tool
def inspect_pdf_page(file_uri: str, page: int) -> str:
    """Return text blocks + bbox coordinates for a single PDF page.

    When to call:
    - You need a precise bbox to anchor an annotation, and `anchor_text` alone
      would be ambiguous (e.g. a checklist where "是" / "否" repeats).
    - General review usually doesn't need this — prefer `anchor_text` in your
      suggestion and let `annotate_pdf` resolve it via text search.

    Args:
        file_uri: Same URI used with ``load_file`` / the pre-loaded document.
        page: 1-indexed page number. One page per call.

    Returns: JSON string with a list of `{block_id, bbox, text}` items.
    Coordinates use `pdf-points-top-left`. Pass a block's `bbox` straight back
    as the `bbox` field of a suggestion in `annotate_pdf`.
    """
    return _inspect_pdf(file_uri, page)


@tool
def inspect_xlsx_sheet(file_uri: str, sheet: str | None = None) -> str:
    """Return a sheet's content (with A1-style column letters) for precise xlsx annotation.

    When to call:
    - Before `annotate_xlsx` on a structured checklist, when you need to know
      which column letter holds 「評估意見 / 意見 / 備註」 etc.
    - You want to confirm a sheet's structure before placing comments.
    - Don't call for ad-hoc edits when you already know exact cell addresses.

    Args:
        file_uri: Same URI used with ``load_file`` / the pre-loaded document.
        sheet: Optional sheet name. Defaults to the active sheet.

    Returns: JSON string of the form
    ``{all_sheets, sheet, dimensions, rows:[{row, cells:[{col, text}]}], truncated}``.
    Empty cells are omitted. You decide which row is the header and which
    column is the "opinion" column from the content; no server-side guessing.
    """
    return _inspect_xlsx(file_uri, sheet)


@tool
def search_knowledge_base(query: str) -> str:
    """Retrieve relevant passages from the enterprise Bedrock Knowledge Base.

    Use for company policy, SOP, past case lookups. Prefer this over web_search.

    Args:
        query: A natural-language query.
    """
    return json.dumps(_kb(query))


@tool
def web_search(query: str) -> str:
    """Search the public web (Tavily) for information not in the KB.

    Use only when the knowledge base does not have the information.

    Args:
        query: A natural-language web search query.
    """
    return json.dumps(_web(query))


def _parse_suggestions(suggestions_json: str) -> tuple[list | None, str | None]:
    try:
        data = json.loads(suggestions_json)
    except json.JSONDecodeError as e:
        return None, f"ERROR: suggestions_json is not valid JSON ({e}). Re-emit with a valid JSON array."
    if not isinstance(data, list):
        return None, "ERROR: suggestions_json must decode to a list. Wrap your suggestions in []."
    return data, None


@tool
def annotate_pdf(file_uri: str, suggestions_json: str) -> str:
    """Write sticky-note + highlight annotations onto a PDF and save the revised file.

    When to call: the user asked you to review / audit / mark / annotate / comment
    on a PDF. Do NOT call this for pure summary, explanation, or chat.

    Args:
        file_uri: Original PDF location (s3:// or local path).
        suggestions_json: JSON string decoding to a list of suggestions.

    Each suggestion MUST contain these keys (canonical key names, do not rename):
      - finding_id: str        unique id within this file (e.g. "f1", "f2")
      - page: int              1-indexed page number
      - severity: str          one of "pass" | "info" | "warning" | "critical"
      - text: str              ⚠️ KEY MUST BE "text" — body content in 繁體中文, ≤3 sentences.
                                NOT "comment" / "body" / "content" — use "text" exactly.

    AND at least one of the following positioning fields:
      - bbox: [x0,y0,x1,y1]    most precise; get from get_pdf_text_positions
      - anchor_text: str       ≥8 chars, must be unique on the page; include surrounding
                                identifiers e.g. "R-03 風險評估等級：低"
      - region: "full_page" | "top_half" | "bottom_half"   visual elements with no text

    Resolution priority: bbox > anchor_text > region. If none resolve, the note
    falls back to the page's top-left corner.

    Returns: URI (local path or s3://...) of the revised PDF.
    """
    suggestions, err = _parse_suggestions(suggestions_json)
    if err:
        return err
    return _annotate_pdf(file_uri, suggestions)


@tool
def annotate_xlsx(file_uri: str, suggestions_json: str) -> str:
    """Attach cell comments to an Excel workbook and save the revised file.

    When to call: the user asked you to review / audit / mark / annotate / comment
    on an Excel sheet (e.g. checklist audit). Do NOT call for summary or chat.

    Args:
        file_uri: Original xlsx location (s3:// or local path).
        suggestions_json: JSON string decoding to a list of suggestions.

    Each suggestion MUST contain these keys (canonical key names, do not rename):
      - finding_id: str        unique id within this file
      - sheet: str             worksheet name (defaults to first sheet if absent)
      - cell: str              A1-style address, e.g. "B5"
      - severity: str          one of "pass" | "info" | "warning" | "critical"
      - text: str              ⚠️ KEY MUST BE "text" — body content in 繁體中文, ≤3 sentences.
                                NOT "comment" / "body" / "content" — use "text" exactly.

    Returns: URI (local path or s3://...) of the revised xlsx.
    """
    suggestions, err = _parse_suggestions(suggestions_json)
    if err:
        return err
    return _annotate_xlsx(file_uri, suggestions)


def _load_system_prompt_template() -> str:
    """Read the XML-tagged system prompt from disk.

    The file is read once per process (module-level cache below).
    """
    return _PROMPT_PATH.read_text(encoding="utf-8")


_SYSTEM_PROMPT_TEMPLATE = _load_system_prompt_template()


def _build_memory_hooks(actor_id: str | None, session_id: str) -> list:
    """Attach the memory write-hook only when both MEMORY_ID and actor_id
    are present. Without an actor we cannot satisfy the AgentCore CreateEvent
    API contract, and we explicitly want anonymous callers to skip memory
    rather than share a default identity.
    """
    memory_id = os.getenv("MEMORY_ID")
    if not actor_id or not memory_id:
        log.info(
            "memory disabled for this invocation (actor_id=%s, MEMORY_ID set=%s)",
            actor_id, bool(memory_id),
        )
        return []
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2"
    return [DocReviewMemoryHooks(
        memory_id=memory_id,
        actor_id=actor_id,
        session_id=session_id,
        region=region,
    )]


def _build_system_prompt(actor_id: str | None) -> str:
    """Inject the actor's stored USER_PREFERENCE records into the prompt template.

    Returns the template with the {PREFERENCES_BLOCK} placeholder replaced by
    either a bullet list of preferences or a placeholder line. Never raises —
    if memory retrieval fails or no actor_id was provided, prefs is [] and the
    agent runs as if this user has no prior preferences.
    """
    prefs = load_user_preferences(actor_id) if actor_id else []
    if prefs:
        log.info("injected %d preference(s) for actor=%s", len(prefs), actor_id)
    return _SYSTEM_PROMPT_TEMPLATE.replace(
        "{PREFERENCES_BLOCK}", format_preferences_block(prefs)
    )


def build_agent(actor_id: str | None, session_id: str) -> Agent:
    """Build a fresh Agent per invocation to avoid carrying conversation history
    between unrelated requests."""
    return Agent(
        model=load_model(),
        system_prompt=_build_system_prompt(actor_id),
        tools=[
            load_file,
            inspect_pdf_page,
            inspect_xlsx_sheet,
            search_knowledge_base,
            web_search,
            annotate_pdf,
            annotate_xlsx,
        ],
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
    """Validate optional `files` list. Returns (files_or_None, error_or_None).

    `type` in each entry is optional — if absent we infer it from the URI's
    extension. Explicit `type` (if provided) must still be 'pdf' or 'xlsx' and
    overrides the inference.
    """
    if raw_files is None:
        return None, None
    if not isinstance(raw_files, list):
        return None, "files must be a list of {uri, type?} objects"
    if not raw_files:
        return [], None
    normalized: list[dict] = []
    for i, f in enumerate(raw_files):
        if not isinstance(f, dict):
            return None, f"files[{i}] must be an object"
        uri = f.get("uri")
        if not uri:
            return None, f"files[{i}] requires 'uri'"
        ftype = f.get("type") or _infer_type(uri)
        if not ftype:
            return None, (
                f"files[{i}]: cannot infer type from uri {uri!r}; "
                "either rename with .pdf/.xlsx extension or pass 'type' explicitly"
            )
        if ftype not in ("pdf", "xlsx"):
            return None, f"files[{i}].type must be 'pdf' or 'xlsx', got {ftype!r}"
        normalized.append({"uri": uri, "type": ftype})
    return normalized, None


def _validate_payload(payload: dict) -> tuple[dict | None, str | None]:
    """Strict validation of the payload shape."""
    if not isinstance(payload, dict):
        return None, "payload must be a JSON object"

    # Reject legacy fields explicitly so old callers fail loudly.
    legacy_fields = [k for k in ("file_uri", "file_type", "task", "mode") if k in payload]
    if legacy_fields:
        return None, (
            f"legacy payload fields not supported: {legacy_fields}. "
            "use {actor_id?, instruction, files?} instead."
        )

    # actor_id is OPTIONAL. Without it the request is processed in stateless
    # mode (no preference injection, no event write, no history recall).
    actor_id_raw = payload.get("actor_id")
    if actor_id_raw is None:
        actor_id: str | None = None
    elif isinstance(actor_id_raw, str) and actor_id_raw.strip():
        actor_id = actor_id_raw.strip()
    else:
        return None, "actor_id, when provided, must be a non-empty string"

    files, err = _normalize_files(payload.get("files"))
    if err:
        return None, err

    # instruction is REQUIRED. Attaching files alone is no longer enough — the
    # caller must say what they want done (summarise, review, audit, etc.).
    instruction_raw = payload.get("instruction")
    if not isinstance(instruction_raw, str) or not instruction_raw.strip():
        return None, "instruction is required and must be a non-empty string"
    instruction = instruction_raw.strip()

    raw_session = payload.get("session_id")
    is_continuation = isinstance(raw_session, str) and bool(raw_session.strip())
    session_id = raw_session.strip() if is_continuation else _new_session_id()
    if raw_session is not None and not is_continuation:
        return None, "session_id, when provided, must be a non-empty string"

    return {
        "actor_id": actor_id,                    # str or None
        "instruction": instruction,
        "files": files or [],
        "session_id": session_id,
        "is_continuation": is_continuation,
        "memory_enabled": actor_id is not None,
    }, None


def _build_prompt(req: dict, history_block: str = "") -> str:
    """Compose the text portion of the user-turn prompt.

    Returned as the final ContentBlock after any pre-loaded document blocks.
    The file uris are spelled out in plain text here so that:
      (1) the agent can refer to them when calling tools like load_file later
      (2) memory hooks capture them — first-turn URIs are recoverable from
          <recent_history> on subsequent turns.
    """
    lines = [
        f"actor_id: {req['actor_id']}",
        f"session_id: {req['session_id']}",
        f"files_count: {len(req['files'])}",
    ]
    for i, f in enumerate(req["files"], 1):
        lines.append(f"  file {i}: uri={f['uri']}  type={f['type']}")
    lines.append("")

    if history_block:
        lines.append("這是同一個 session 過去幾輪的對話（請以這些上下文回應當前指令）：")
        lines.append("<recent_history>")
        lines.append(history_block)
        lines.append("</recent_history>")
        lines.append("")

    lines.append("使用者指令（請依此 instruction 自行決定要呼叫哪些工具）：")
    lines.append(req["instruction"])
    lines.append("")
    lines.append(f"完成後請以 fenced ```json 區塊回覆，且 session_id 欄位填 {req['session_id']!r}。")
    return "\n".join(lines)


def _build_content_blocks(req: dict, history_block: str) -> list[dict]:
    """Compose the multimodal user-turn input as a list of Strands ContentBlocks.

    Each file in the payload becomes a `document` content block with the file's
    bytes inlined. The final block is the text prompt.

    NOTE: Bedrock's Converse API does NOT accept ``s3Location`` as a
    DocumentSource on Anthropic Claude models — files must be inlined as bytes.
    We download from s3 here so the rest of the pipeline doesn't need to know.
    """
    blocks: list[dict] = []
    for f in req["files"]:
        uri = f["uri"]
        display = os.path.basename(uri) if uri.startswith("s3://") else Path(uri).name
        stem = display.rsplit(".", 1)[0]
        blocks.append({
            "document": {
                "format": f["type"],
                "name": stem,
                "source": {"bytes": _read_uri_bytes(uri)},
            }
        })
    blocks.append({"text": _build_prompt(req, history_block)})
    return blocks


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
        is_continuation=req["is_continuation"],
        memory_enabled=req["memory_enabled"],
    )

    # Cold-start prewarm: pull Tavily key from env (local) or AgentCore Identity (cloud).
    try:
        await _prewarm_tavily_key()
    except Exception as e:
        log.warning(f"Tavily key prewarm failed (web_search will error if used): {e}")

    # Block C: load short-term recall when caller is continuing an existing session.
    # Skipped entirely when actor_id is missing — get_last_k_turns requires it
    # and an anonymous "history" makes no sense.
    history_block = ""
    if req["actor_id"] and req["is_continuation"]:
        recent = load_recent_turns(req["actor_id"], req["session_id"], k=5)
        history_block = format_recent_turns_block(recent)
        log.info(
            "loaded %d recent turn(s) for session=%s (continuation)",
            len(recent), req["session_id"],
        )

    seen_tools: set[str] = set()
    final_text_parts: list[str] = []
    last_message: dict | None = None

    try:
        agent = build_agent(req["actor_id"], req["session_id"])
        content_blocks = _build_content_blocks(req, history_block)
        async for event in agent.stream_async(content_blocks):
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
