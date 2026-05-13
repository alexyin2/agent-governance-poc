"""Multimodal file loader — surface a PDF/Excel into the conversation as a
Bedrock `document` content block so Claude can see it (text + layout + figures).

The agent uses this when it needs to look at a file that wasn't pre-loaded by
the runtime — typically when `<recent_history>` mentions a file from an earlier
turn that the current payload didn't re-attach.

Strands' @tool decorator passes a dict with `status` + `content` straight through
to the model as a tool result, so we can put a {"document": {...}} block here and
Bedrock's Converse adapter renders it as native multimodal input.

S3 sources are downloaded into bytes here — Bedrock's Converse API does NOT
accept ``s3Location`` for ``DocumentSource`` on Anthropic Claude models (only
on Nova). The whole file is inlined as ``{"bytes": ...}`` regardless of origin.
"""

import os
from pathlib import Path
from typing import Any

import boto3


_PDF_EXTS = (".pdf",)
_XLSX_EXTS = (".xlsx", ".xlsm", ".xls")


def read_uri_bytes(file_uri: str) -> bytes:
    """Read the full contents of an s3:// URI or local path into memory.

    Shared helper for the loader tool and the runtime's pre-load path so the
    download logic lives in one place.
    """
    if file_uri.startswith("s3://"):
        bucket, _, key = file_uri[5:].partition("/")
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    return Path(file_uri).read_bytes()


def infer_file_type(file_uri: str) -> str | None:
    """Return ``"pdf"`` or ``"xlsx"`` based on the URI's extension; None if unknown.

    Shared helper used by both the loader tool and the runtime's pre-load path
    so the inference rule lives in exactly one place.
    """
    low = file_uri.lower()
    if low.endswith(_PDF_EXTS):
        return "pdf"
    if low.endswith(_XLSX_EXTS):
        return "xlsx"
    return None


def load_file(file_uri: str) -> dict[str, Any]:
    """Load a PDF or Excel file into the conversation so you can see it.

    When to call:
    - The user references a file that was not attached in this turn's payload
      (typically from <recent_history>).
    - You need to re-examine a previously discussed file at a deeper level.

    Do NOT call this for files already in this turn's payload — those are
    pre-loaded automatically before you start.

    File type is inferred from the URI extension (.pdf / .xlsx / .xlsm / .xls).

    Args:
        file_uri: An ``s3://bucket/key`` URI or a local filesystem path.

    Returns: a tool result containing the document block (so you can see it) +
    a short confirmation text. The model receives this as native multimodal input.
    """
    file_type = infer_file_type(file_uri)
    if file_type is None:
        return {
            "status": "error",
            "content": [{"text": f"cannot infer file type from URI: {file_uri!r}. Expected .pdf / .xlsx."}],
        }

    if file_uri.startswith("s3://"):
        display_name = os.path.basename(file_uri)
    else:
        path = Path(file_uri)
        if not path.exists():
            return {
                "status": "error",
                "content": [{"text": f"file not found: {file_uri}"}],
            }
        display_name = path.name

    try:
        source = {"bytes": read_uri_bytes(file_uri)}
    except Exception as e:
        return {
            "status": "error",
            "content": [{"text": f"failed to read {file_uri}: {e}"}],
        }

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
