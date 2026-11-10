"""Qiita connector (official API v2, personal read-only token) -> unified documents.

Needs QIITA_TOKEN in .env (scope read_qiita only). 1000 requests/hour with a token; each request
returns up to 100 full articles, so one query costs a handful of requests.
"""
from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

import requests

import documents
import filters
import llm

log = logging.getLogger("qiita")
API_URL = "https://qiita.com/api/v2/items"
MAX_PAGES = 100  # API limit: page 1..100 with per_page up to 100 (10,000 items per query)


def _get(params: dict, token: str, delay: float, attempts: int = 4) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(API_URL, params=params, headers=headers, timeout=60)
            if response.status_code == 429:  # rate limit: wait for the next hour window
                wait = 60 * (attempt + 1)
                log.warning("Qiita rate limit hit, sleeping %ds", wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            time.sleep(delay)
            return response
        except requests.RequestException as exc:
            if attempt == attempts:
                raise
            wait = 2 ** attempt + random.uniform(0, 1)
            log.warning("Qiita GET failed (%s), retry %d/%d in %.0fs", exc, attempt, attempts, wait)
            time.sleep(wait)
    raise RuntimeError("Qiita: rate limit did not clear")


def search(query: dict, since: datetime, token: str, delay: float) -> list[dict]:
    """All articles with the tag since `since` and at least min_stocks stocks (newest first)."""
    q = f"tag:{query['tag']} created:>={since.strftime('%Y-%m-%d')}"
    if query.get("min_stocks"):
        q += f" stocks:>={query['min_stocks']}"
    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = _get({"query": q, "per_page": 100, "page": page}, token, delay)
        batch = response.json()
        items.extend(batch)
        total = int(response.headers.get("Total-Count", len(items)))
        log.info("[qiita:%s] page %d: %d/%d", query["name"], page, len(items), total)
        if not batch or len(items) >= total:
            break
    else:
        log.warning("[qiita:%s] hit the %d-page API limit; narrow the query (raise min_stocks)", query["name"], MAX_PAGES)
    return items


def to_entry(item: dict, query_name: str) -> dict:
    return {
        "id": item["id"],
        "title": item.get("title") or "",
        "upload_date": (item.get("created_at") or "")[:10] or None,
        "author": (item.get("user") or {}).get("id"),
        "link": item.get("url"),
        "likes": item.get("likes_count") or 0,
        "stocks": item.get("stocks_count") or 0,
        "tags": [t["name"] for t in item.get("tags") or []],
        "queries": [query_name],
        "body": item.get("body") or "",
    }


def build_document(entry: dict, max_chars: int) -> dict:
    return {
        "source": "qiita",
        "source_id": entry["id"],
        "url": entry["link"],
        "date": entry["upload_date"],
        "author": entry["author"],
        "title": entry["title"],
        "text": entry["body"][:max_chars],
        "engagement": {"likes": entry["likes"], "stocks": entry["stocks"]},
        "meta": {"language": "ja", "tags": entry["tags"], "queries": entry["queries"]},
    }


def collect(source_cfg: dict, limit: int | None = None, dry_run: bool = False,
            only_queries: list[str] | None = None) -> dict:
    llm.load_dotenv()
    token = os.environ.get("QIITA_TOKEN")
    if not token:
        raise SystemExit("QIITA_TOKEN is not set (put it in .env; scope read_qiita only).")
    since = datetime.now(timezone.utc) - timedelta(days=30 * source_cfg["max_age_months"])
    delay = source_cfg.get("sleep_seconds", 0.5)

    merged: dict[str, dict] = {}
    for query in source_cfg["queries"]:
        if only_queries and query["name"] not in only_queries:
            continue
        for item in search(query, since, token, delay):
            entry = to_entry(item, query["name"])
            if entry["id"] in merged:
                merged[entry["id"]]["queries"].append(query["name"])
            else:
                merged[entry["id"]] = entry

    result = filters.apply_layer0(list(merged.values()), filters.load_layer0("qiita"))
    filters.save_layer0("qiita", result)
    for line in filters.format_report("qiita", result["stats"]).splitlines():
        log.info(line)
    kept = sorted(result["kept"], key=lambda e: e["stocks"], reverse=True)
    if limit:
        kept = kept[:limit]
    downloaded = present = 0
    if not dry_run:
        for entry in kept:
            if documents.exists("qiita", entry["id"]):
                present += 1
                continue
            documents.save(build_document(entry, source_cfg.get("max_chars", 60000)))
            downloaded += 1
    return {"candidates": kept, "stats": result["stats"], "downloaded": downloaded,
            "already_present": present, "failed": 0}
