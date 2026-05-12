"""Multimodal file loader — surface a PDF/Excel into the conversation as a
Bedrock `document` content block so Claude can see it (text + layout + figures).

The agent uses this when it needs to look at a file that wasn't pre-loaded by
the runtime — typically when `<recent_history>` mentions a file from an earlier
turn that the current payload didn't re-attach.

Strands' @tool decorator passes a dict with `status` + `content` straight through
to the model as a tool result, so we can put a {"document": {...}} block here and
Bedrock's Converse adapter renders it as native multimodal input.

Files via `s3://` URIs are passed as `{"location": {"type": "s3", "uri": ...}}`
so Bedrock fetches them directly — avoiding a local download round-trip.
"""

import os
from pathlib import Path
from typing import Any


_SUPPORTED_TYPES = {"pdf", "xlsx"}


def load_file(file_uri: str, file_type: str) -> dict[str, Any]:
    """Load a PDF or Excel file into the conversation so you can see it.

    When to call:
    - The user references a file that was not attached in this turn's payload
      (typically from <recent_history>).
    - You need to re-examine a previously discussed file at a deeper level.

    Do NOT call this for files already in this turn's payload — those are
    pre-loaded automatically before you start.

    Args:
        file_uri: An ``s3://bucket/key`` URI or a local filesystem path.
        file_type: ``"pdf"`` or ``"xlsx"``.

    Returns: a tool result containing the document block (so you can see it) +
    a short confirmation text. The model receives this as native multimodal input.
    """
    if file_type not in _SUPPORTED_TYPES:
        return {
            "status": "error",
            "content": [{"text": f"unsupported file_type: {file_type!r}. expected one of {sorted(_SUPPORTED_TYPES)}"}],
        }

    if file_uri.startswith("s3://"):
        source = {"location": {"type": "s3", "uri": file_uri}}
        display_name = os.path.basename(file_uri)
    else:
        path = Path(file_uri)
        if not path.exists():
            return {
                "status": "error",
                "content": [{"text": f"file not found: {file_uri}"}],
            }
        source = {"bytes": path.read_bytes()}
        display_name = path.name

    # Strip extension for the document `name` field — Bedrock disallows dots there.
    stem = display_name.rsplit(".", 1)[0]

    return {
        "status": "success",
        "content": [
            {
                "document": {
                    "format": file_type,
                    "name": stem,
                    "source": source,
                }
            },
            {"text": f"已載入 {display_name}，請查看上方檔案內容後繼續分析。"},
        ],
    }
