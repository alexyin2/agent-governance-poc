"""Write the agent's suggestions back into the source file as annotations / comments."""

import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
import fitz  # PyMuPDF
from openpyxl import load_workbook
from openpyxl.comments import Comment

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "outputs")


def _timestamp() -> str:
    """UTC timestamp suitable for file names: YYYYMMDDTHHMMSSZ."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_local(file_uri: str) -> str:
    if file_uri.startswith("s3://"):
        without = file_uri[5:]
        bucket, _, key = without.partition("/")
        tmp_dir = tempfile.mkdtemp(prefix="agent-poc-write-")
        local = os.path.join(tmp_dir, os.path.basename(key))
        boto3.client("s3").download_file(bucket, key, local)
        return local
    return file_uri


def _output_path(src: str) -> str:
    base = os.path.basename(src)
    name, ext = os.path.splitext(base)
    # Timestamp prevents repeat invocations from clobbering each other in
    # outputs/ (or in S3, where overwrites are silent).
    out_name = f"{name}_revised_{_timestamp()}{ext}"
    # Cloud path (S3_BUCKET set): write to tempdir; container cwd is often read-only.
    # _maybe_upload() will pick the file up and PutObject to s3://bucket/outputs/.
    if os.getenv("S3_BUCKET"):
        tmp_dir = tempfile.mkdtemp(prefix="agent-poc-out-")
        return os.path.join(tmp_dir, out_name)
    # Local dev path: write under repo's outputs/ for easy inspection.
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, out_name)


def _maybe_upload(local: str) -> str:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return local
    key = f"outputs/{os.path.basename(local)}"
    boto3.client("s3").upload_file(local, bucket, key)
    return f"s3://{bucket}/{key}"


def write_revised_pdf(file_uri: str, suggestions: List[Dict[str, Any]]) -> str:
    """Add a sticky-note annotation per suggestion. Each suggestion needs:
    {page: int (1-indexed), bbox: [x0,y0,x1,y1] (optional), text: str}
    """
    local = _ensure_local(file_uri)
    doc = fitz.open(local)
    for idx, s in enumerate(suggestions):
        page_idx = max(0, int(s.get("page", 1)) - 1)
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]
        bbox = s.get("bbox")
        body = s.get("text", "")
        if bbox and len(bbox) == 4:
            page.add_highlight_annot(fitz.Rect(*bbox))
            page.add_text_annot(fitz.Point(bbox[0], bbox[1]), body)
        else:
            page.add_text_annot(fitz.Point(20, 20 + 15 * idx), body)
    out = _output_path(local)
    doc.save(out)
    doc.close()
    return _maybe_upload(out)


def _comment_size(text: str) -> tuple[int, int]:
    """Pick a comment box big enough for the text (Excel default 100x150 px is too small).

    Heuristic: ~24 CJK chars per line, ~18px line height. Min 400x200, max 600x600.
    """
    line_chars = 24
    lines = max(1, sum(max(1, -(-len(part) // line_chars)) for part in text.split("\n")))
    width = 480
    height = max(200, min(600, 40 + lines * 22))
    return width, height


def write_revised_xlsx(file_uri: str, suggestions: List[Dict[str, Any]]) -> str:
    """Attach a comment per suggestion. Each needs {sheet: str, cell: str (e.g. 'A1'), text: str}."""
    local = _ensure_local(file_uri)
    wb = load_workbook(local)
    for s in suggestions:
        sheet = s.get("sheet") or wb.sheetnames[0]
        cell = s.get("cell")
        body = s.get("text", "")
        if not cell or sheet not in wb.sheetnames:
            continue
        comment = Comment(body, "AgentReviewer")
        comment.width, comment.height = _comment_size(body)
        wb[sheet][cell].comment = comment
    out = _output_path(local)
    wb.save(out)
    return _maybe_upload(out)


def write_revised_file(file_uri: str, file_type: str, suggestions: List[Dict[str, Any]]) -> str:
    if file_type == "pdf":
        return write_revised_pdf(file_uri, suggestions)
    if file_type in ("xlsx", "excel"):
        return write_revised_xlsx(file_uri, suggestions)
    raise ValueError(f"Unsupported file_type: {file_type}")
