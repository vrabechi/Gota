"""Hacker News connector (Algolia search + items endpoints) -> unified documents."""
from __future__ import annotations

import html
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests

import documents
import filters
import llm

log = logging.getLogger("hackernews")
SEARCH_CACHE = documents.ROOT / "data" / "cache" / "hackernews_search.json"

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
HN_ITEM_PAGE = "https://news.ycombinator.com/item?id={id}"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _get(url: str, params: dict | None, sleep: float, attempts: int = 4) -> dict:
    """GET JSON with exponential backoff + jitter."""
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            time.sleep(sleep)
            return response.json()
        except requests.RequestException as exc:
            if attempt == attempts:
                raise
            delay = 2 ** attempt + random.uniform(0, 1)
            log.warning("GET %s failed (%s), retry %d/%d in %.0fs", url, exc, attempt, attempts, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")


def clean_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<p>", "\n", text)
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", text))).strip()


def _term_regex(term: str) -> re.Pattern:
    """Whole-word match (Algolia matches prefixes and typos: 'ARR' also returns 'arrest')."""
    return re.compile(r"\b" + re.escape(term.strip('"')) + r"\b", re.IGNORECASE)


def _load_search_cache() -> dict:
    return llm.read_json(SEARCH_CACHE) or {}


def search_query(query: dict, cfg: dict, since: datetime, until: datetime) -> list[dict]:
    """One (tags, term) search split in time windows (each returns at most 1000 hits).

    With `terms`, hits are kept only if the term appears as a whole word in the title or post text.
    Windows that ended more than a day ago are cached on disk (data/cache/hackernews_search.json).
    """
    cache = _load_search_cache()
    results: list[dict] = []
    window = timedelta(days=cfg["window_days"])
    final_before = until - timedelta(days=1)
    start = since
    terms = query.get("terms") or [None]
    while start < until:
        end = min(start + window, until)
        for term in terms:
            cache_key = f"{query['tags']}|{query['min_points']}|{term}|{int(start.timestamp())}|{int(end.timestamp())}"
            if cache_key in cache:
                results.extend(cache[cache_key])
                continue
            params = {
                "tags": query["tags"],
                "numericFilters": f"points>={query['min_points']},created_at_i>={int(start.timestamp())},"
                                  f"created_at_i<{int(end.timestamp())}",
                "hitsPerPage": 1000,
            }
            if term:
                params["query"] = term
            data = _get(SEARCH_URL, params, cfg["sleep_seconds"])
            hits = data.get("hits", [])
            if data.get("nbHits", 0) > len(hits):
                log.warning("[%s] window %s truncated: %d hits, kept %d (reduce window_days)",
                            query["name"], start.date(), data["nbHits"], len(hits))
            if term:
                regex = _term_regex(term)
                hits = [h for h in hits if regex.search(f"{h.get('title') or ''} {clean_html(h.get('story_text'))}")]
            results.extend(hits)
            if end <= final_before:
                cache[cache_key] = hits
        start = end
    llm.write_json(SEARCH_CACHE, cache)
    return results


def to_entry(hit: dict, query_name: str) -> dict:
    """Candidate entry understood by filters.apply_layer0."""
    return {
        "id": hit["objectID"],
        "title": hit.get("title") or "",
        "score": hit.get("points") or 0,
        "num_comments": hit.get("num_comments") or 0,
        "upload_date": (hit.get("created_at") or "")[:10] or None,
        "author": hit.get("author"),
        "external_url": hit.get("url"),
        "story_text": hit.get("story_text"),
        "queries": [query_name],
    }


def build_document(entry: dict, item: dict, cfg: dict) -> dict:
    comments = []
    for child in (item.get("children") or []):
        text = clean_html(child.get("text"))
        if text and child.get("author"):
            comments.append(f"- {child['author']}: {text[:cfg['comment_chars']]}")
        if len(comments) >= cfg["comments_per_item"]:
            break
    post_text = clean_html(entry.get("story_text"))
    parts = []
    if post_text:
        parts.append(f"POST TEXT:\n{post_text}")
    if entry.get("external_url"):
        parts.append(f"LINKED URL (content not fetched): {entry['external_url']}")
    if comments:
        parts.append("TOP-LEVEL COMMENTS:\n" + "\n".join(comments))
    return {
        "source": "hackernews",
        "source_id": entry["id"],
        "url": HN_ITEM_PAGE.format(id=entry["id"]),
        "date": entry["upload_date"],
        "author": entry.get("author"),
        "title": entry["title"],
        "text": "\n\n".join(parts),
        "engagement": {"score": entry["score"], "comments": entry["num_comments"]},
        "meta": {"external_url": entry.get("external_url"), "queries": entry["queries"],
                 "comments_kept": len(comments)},
    }


def collect(source_cfg: dict, limit: int | None = None, dry_run: bool = False,
            only_queries: list[str] | None = None) -> dict:
    """Search, apply Layer 0, then download items (comments) for the survivors. Resumable."""
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=30 * source_cfg["max_age_months"])

    merged: dict[str, dict] = {}
    for query in source_cfg["queries"]:
        if only_queries and query["name"] not in only_queries:
            continue
        hits = search_query(query, source_cfg, since, until)
        log.info("[hackernews:%s] %d hits", query["name"], len(hits))
        for hit in hits:
            entry = to_entry(hit, query["name"])
            if entry["id"] in merged:
                if query["name"] not in merged[entry["id"]]["queries"]:
                    merged[entry["id"]]["queries"].append(query["name"])
            else:
                merged[entry["id"]] = entry
    entries = list(merged.values())

    result = filters.apply_layer0(entries, filters.load_layer0("hackernews"))
    filters.save_layer0("hackernews", result)
    for line in filters.format_report("hackernews", result["stats"]).splitlines():
        log.info(line)
    kept = sorted(result["kept"], key=lambda e: e["score"], reverse=True)
    if limit:
        kept = kept[:limit]
    if dry_run:
        return {"candidates": kept, "stats": result["stats"], "downloaded": 0, "already_present": 0, "failed": 0}

    downloaded = present = failed = 0
    for entry in kept:
        if documents.exists("hackernews", entry["id"]):
            present += 1
            continue
        try:
            item = _get(ITEM_URL.format(id=entry["id"]), None, source_cfg["sleep_seconds"])
        except requests.RequestException as exc:
            log.error("[hackernews] FAILED item %s: %s", entry["id"], exc)
            failed += 1
            continue
        documents.save(build_document(entry, item, source_cfg))
        downloaded += 1
        log.info("[hackernews] saved %s (%d pts) %s", entry["id"], entry["score"], entry["title"])
    return {"candidates": kept, "stats": result["stats"], "downloaded": downloaded,
            "already_present": present, "failed": failed}
