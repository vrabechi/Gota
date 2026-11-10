"""WordPress blog connector (public REST API: /wp-json/wp/v2/posts) -> unified documents.

Source name is "blog"; source_id is "<site>-<post id>" so ids from different sites never collide.
Two passes keep it resumable and polite: (1) list ids/dates/titles inside the date window (no body),
(2) fetch bodies only for posts that survive Layer 0 and are not on disk yet.
"""
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

log = logging.getLogger("wordpress")
USER_AGENT = "Mozilla/5.0 (compatible; personal-research-pipeline)"
SOURCE = "blog"

_DROP_BLOCKS = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_BREAKS = re.compile(r"</(p|h[1-6]|li|div|blockquote|tr)>|<br\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


def html_to_text(raw: str | None) -> str:
    """Rendered WordPress HTML -> plain text with paragraph breaks kept."""
    if not raw:
        return ""
    text = _BREAKS.sub("\n", _DROP_BLOCKS.sub("", raw))
    text = html.unescape(_TAGS.sub("", text)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _get(url: str, params: dict, delay: float, attempts: int = 4) -> requests.Response:
    """GET with exponential backoff + jitter; always waits `delay` seconds afterwards (crawl-delay)."""
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=60)
            response.raise_for_status()
            time.sleep(delay)
            return response
        except requests.RequestException as exc:
            if attempt == attempts:
                raise
            wait = max(delay, 2 ** attempt) + random.uniform(0, 1)
            log.warning("GET %s failed (%s), retry %d/%d in %.0fs", url, exc, attempt, attempts, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def list_posts(site: dict, since: datetime) -> list[dict]:
    """Id, date, link and title of every post published since `since` (newest first)."""
    url = site["base_url"].rstrip("/") + "/wp-json/wp/v2/posts"
    delay = site.get("crawl_delay_seconds", 1)
    params = {"per_page": 100, "orderby": "date", "order": "desc",
              "after": since.strftime("%Y-%m-%dT%H:%M:%S"), "_fields": "id,date_gmt,link,title"}
    posts: list[dict] = []
    page, total_pages = 1, 1
    while page <= total_pages:
        response = _get(url, {**params, "page": page}, delay)
        total_pages = int(response.headers.get("X-WP-TotalPages", 1))
        posts.extend(response.json())
        log.info("[blog:%s] listing page %d/%d (%d posts so far)", site["name"], page, total_pages, len(posts))
        page += 1
    return posts


def to_entry(site: dict, post: dict) -> dict:
    """Candidate entry understood by filters.apply_layer0."""
    return {
        "id": post["id"],
        "title": html.unescape(re.sub(r"<[^>]+>", "", (post.get("title") or {}).get("rendered", ""))),
        "upload_date": (post.get("date_gmt") or "")[:10] or None,
        "author": site.get("display_name", site["name"]),
        "link": post.get("link"),
        "site": site["name"],
    }


def fetch_bodies(site: dict, ids: list[int]) -> list[dict]:
    """Full posts for the given ids, up to 100 per request."""
    url = site["base_url"].rstrip("/") + "/wp-json/wp/v2/posts"
    delay = site.get("crawl_delay_seconds", 1)
    posts: list[dict] = []
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        params = {"include": ",".join(map(str, batch)), "per_page": 100, "_fields": "id,date_gmt,link,title,content"}
        posts.extend(_get(url, params, delay).json())
        log.info("[blog:%s] bodies %d/%d", site["name"], min(start + 100, len(ids)), len(ids))
    return posts


def build_document(site: dict, entry: dict, post: dict) -> dict:
    return {
        "source": SOURCE,
        "source_id": f"{site['name']}-{entry['id']}",
        "url": entry["link"],
        "date": entry["upload_date"],
        "author": entry["author"],
        "title": entry["title"],
        "text": html_to_text((post.get("content") or {}).get("rendered")),
        "engagement": {},
        "meta": {"site": site["name"], "wp_post_id": entry["id"]},
    }


def collect(source_cfg: dict, limit: int | None = None, dry_run: bool = False,
            only_sites: list[str] | None = None) -> dict:
    """List posts in the window, apply Layer 0, then download bodies of the new survivors."""
    since = datetime.now(timezone.utc) - timedelta(days=30 * source_cfg["max_age_months"])
    totals = {"candidates": [], "downloaded": 0, "already_present": 0, "failed": 0}
    for site in source_cfg["sites"]:
        if only_sites and site["name"] not in only_sites:
            continue
        try:
            entries = [to_entry(site, p) for p in list_posts(site, since)]
        except requests.RequestException as exc:
            log.error("[blog:%s] listing failed: %s", site["name"], exc)
            totals["failed"] += 1
            continue
        result = filters.apply_layer0(entries, filters.load_layer0("blog"))
        filters.save_layer0(f"blog_{site['name']}", result)
        for line in filters.format_report(f"blog:{site['name']}", result["stats"]).splitlines():
            log.info(line)
        kept = result["kept"][:limit] if limit else result["kept"]
        totals["candidates"].extend(kept)
        if dry_run:
            continue

        by_id = {e["id"]: e for e in kept}
        missing = [i for i, e in by_id.items() if not documents.exists(SOURCE, f"{site['name']}-{i}")]
        totals["already_present"] += len(kept) - len(missing)
        if not missing:
            continue
        try:
            posts = fetch_bodies(site, missing)
        except requests.RequestException as exc:
            log.error("[blog:%s] body download failed: %s", site["name"], exc)
            totals["failed"] += len(missing)
            continue
        for post in posts:
            doc = build_document(site, by_id[post["id"]], post)
            if not doc["text"]:
                totals["failed"] += 1
                continue
            documents.save(doc)
            totals["downloaded"] += 1
    return totals
