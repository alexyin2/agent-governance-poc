"""Bedrock Knowledge Base retrieval wrapper."""

import os
from typing import Any, Dict, List

import boto3
from dotenv import load_dotenv

load_dotenv()


def search_knowledge_base(query: str, max_results: int = 5, kb_id: str | None = None) -> List[Dict[str, Any]]:
    """Retrieve relevant chunks from the configured Bedrock KB.

    Returns a list of {text, source, score}.
    """
    kb_id = kb_id or os.getenv("KB_ID")
    if not kb_id:
        raise RuntimeError("KB_ID not configured (set in .env)")

    region = os.getenv("AWS_REGION", "us-east-1")
    client = boto3.client("bedrock-agent-runtime", region_name=region)
    resp = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": max_results}},
    )
    out = []
    for r in resp.get("retrievalResults", []):
        out.append({
            "text": r.get("content", {}).get("text", ""),
            "source": r.get("location", {}),
            "score": r.get("score"),
        })
    return out
