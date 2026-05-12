"""Per-page coordinate lookup for PDF annotation.

`load_file` lets the agent SEE a PDF (text + visuals via Bedrock document block),
but visual models cannot reliably emit precise pixel coordinates. When the agent
needs a precise bbox to anchor an annotation — e.g. a checklist where the same
answer text ("是" / "否") repeats many times — it calls this tool to look up the
text-block bbox for a specific page.

One page per call by design: the agent should already know which page it cares
about (from the document view), and limiting to one page keeps tool result size
bounded.
"""

import json
import os
import tempfile

import boto3
import fitz  # PyMuPDF


def _resolve_uri(file_uri: str) -> str:
    """Download from S3 if needed, return a local path."""
    if file_uri.startswith("s3://"):
        bucket, _, key = file_uri[5:].partition("/")
        tmp_dir = tempfile.mkdtemp(prefix="agent-poc-inspect-")
        local = os.path.join(tmp_dir, os.path.basename(key))
        boto3.client("s3").download_file(bucket, key, local)
        return local
    return file_uri


def inspect_pdf_page(file_uri: str, page: int) -> str:
    """Return text blocks + bbox coordinates for a single page of a PDF.

    When to call:
    - You need a precise bbox to annotate a specific position and `anchor_text`
      alone would be ambiguous (e.g. checklist with repeated "是" answers).
    - General review usually doesn't need this — prefer `anchor_text` in your
      suggestion and let `annotate_pdf` resolve it via text search.

    Args:
        file_uri: Same URI used with ``load_file`` / pre-loaded document.
        page: 1-indexed page number. One page per call.

    Returns: JSON string with shape::

        {
          "page": 3,
          "width": 595.0,
          "height": 842.0,
          "blocks": [
            {"block_id": "p3_b0", "bbox": [x0, y0, x1, y1], "text": "..."},
            ...
          ]
        }

    Coordinates use the ``pdf-points-top-left`` system (origin at top-left, units
    in PDF points). Pass any block's ``bbox`` straight back as the ``bbox`` field
    of a suggestion in ``annotate_file``.
    """
    local = _resolve_uri(file_uri)
    doc = fitz.open(local)
    try:
        page_idx = max(0, page - 1)
        if page_idx >= len(doc):
            return json.dumps({
                "error": f"page {page} out of range; PDF has {len(doc)} pages",
            })
        p = doc[page_idx]
        blocks = []
        for i, b in enumerate(p.get_text("blocks")):
            x0, y0, x1, y1, text, *_ = b
            text = text.strip()
            if text:
                blocks.append({
                    "block_id": f"p{page}_b{i}",
                    "bbox": [x0, y0, x1, y1],
                    "text": text,
                })
        return json.dumps(
            {
                "page": page,
                "width": p.rect.width,
                "height": p.rect.height,
                "blocks": blocks,
            },
            ensure_ascii=False,
        )
    finally:
        doc.close()
