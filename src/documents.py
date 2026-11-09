"""Unified document format shared by every source.

Document keys:
  source      "youtube" | "hackernews" | ...
  source_id   id inside the source (YouTube video id, HN item id...)
  url, date (ISO yyyy-mm-dd), author, title, text
  engagement  dict of numbers (views, score, comments...)
  meta        source specific extras (channel, duration, description...)

YouTube transcripts keep living in data/transcripts/<channel>/<id>.json (written by download.py)
and are converted on the fly. Other sources are stored in data/documents/<source>/<source_id>.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import llm

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS_DIR = ROOT / "data" / "transcripts"
DOCUMENTS_DIR = ROOT / "data" / "documents"


def from_youtube(record: dict, channel_slug: str) -> dict:
    return {
        "source": "youtube",
        "source_id": record["video_id"],
        "url": record.get("url"),
        "date": record.get("upload_date"),
        "author": record.get("channel"),
        "title": record.get("title"),
        "text": record.get("transcript_text") or "",
        "engagement": {"views": record.get("view_count")},
        "meta": {
            "channel": record.get("channel"),
            "channel_slug": channel_slug,
            "channel_id": record.get("channel_id"),
            "description": record.get("description"),
            "duration": record.get("duration"),
            "caption_source": record.get("caption_source"),
            "caption_language": record.get("caption_language"),
            "language": record.get("language"),
        },
    }


def doc_key(doc: dict) -> str:
    """Cache/file key. YouTube keeps the bare video id so existing caches stay valid."""
    return doc["source_id"] if doc["source"] == "youtube" else f"{doc['source']}_{doc['source_id']}"


def document_path(source: str, source_id: str) -> Path:
    return DOCUMENTS_DIR / source / f"{source_id}.json"


def save(doc: dict) -> None:
    llm.write_json(document_path(doc["source"], doc["source_id"]), doc)


def exists(source: str, source_id: str) -> bool:
    return document_path(source, source_id).exists()


def load_all(sources: list[str] | None = None, channels: list[str] | None = None) -> list[dict]:
    """All documents of the requested sources (default: every source)."""
    docs: list[dict] = []
    if not sources or "youtube" in sources:
        for path in sorted(TRANSCRIPTS_DIR.glob("*/*.json")):
            if channels and path.parent.name not in channels:
                continue
            docs.append(from_youtube(json.loads(path.read_text(encoding="utf-8")), path.parent.name))
    if DOCUMENTS_DIR.exists():
        for source_dir in sorted(p for p in DOCUMENTS_DIR.iterdir() if p.is_dir()):
            if sources and source_dir.name not in sources:
                continue
            for path in sorted(source_dir.glob("*.json")):
                docs.append(json.loads(path.read_text(encoding="utf-8")))
    return docs
