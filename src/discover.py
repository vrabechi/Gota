"""Discover: find videos from channels we did not list, in two stages.

  search   ytsearch queries (config/queries.yaml) -> flat results -> Layer 0 (profile `discover`)   free
  screen   stage A: cheap Haiku screening of the search metadata (no caption download)             paid
  download stage B: captions of the survivors -> data/transcripts/discover/ (then `triage --channel discover`)
  report   data/channel_candidates.csv: channels behind the survivors (never added to channels.yaml)

Everything is cached and resumable: search results, screening results and downloaded transcripts.
"""
from __future__ import annotations

import csv
import logging
import random
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml
from yt_dlp import YoutubeDL

import documents
import download
import filters
import llm

log = logging.getLogger("discover")
ROOT = Path(__file__).resolve().parent.parent
QUERIES_FILE = ROOT / "config" / "queries.yaml"
DISCOVER_DIR = ROOT / "data" / "discover"
SEARCH_CACHE = ROOT / "data" / "cache" / "discover_search.json"
SCREEN_DIR = DISCOVER_DIR / "screen"
CANDIDATES_CSV = ROOT / "data" / "channel_candidates.csv"

SCREEN_PROMPT = """\
You screen YouTube search results for a research pipeline. The pipeline looks for concrete online \
business models (software products, info products, content/SEO sites, services and agencies, \
marketplaces, acquisitions of small businesses) that a solo technical operator could replicate.

You only see: title, channel, duration, view count and a short snippet of the description. There is \
NO transcript. Judge from that text alone and never assume facts that are not in it. The goal is to \
decide whether downloading the transcript is worth it.

Return JSON with exactly these fields:
- relevance_score: integer 0-10. 0-2: clearly no business content (music, news, generic motivation, ads). \
3-4: generic advice or a get-rich-quick pitch with no named business. 5-6: probably discusses a specific \
business or product. 7-10: clearly a case study or interview about a specific, replicable business, \
ideally with figures in the title or snippet.
- reason: one short sentence.
"""

SCREEN_SCHEMA = {
    "type": "object",
    "properties": {"relevance_score": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["relevance_score", "reason"],
    "additionalProperties": False,
}


def load_config() -> dict:
    return yaml.safe_load(QUERIES_FILE.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------ search


def known_video_ids() -> set[str]:
    """Videos we already have as documents: never search results worth re-screening."""
    return {p.stem for p in documents.TRANSCRIPTS_DIR.glob("*/*.json")}


def known_channels() -> tuple[set[str], set[str]]:
    """(channel ids, lowercase handles) of the channels already configured/downloaded."""
    import json
    ids: set[str] = set()
    for path in documents.TRANSCRIPTS_DIR.glob("*/*.json"):
        if path.parent.name == "discover":
            continue
        channel_id = json.loads(path.read_text(encoding="utf-8")).get("channel_id")
        if channel_id:
            ids.add(channel_id)
    _, channels = download.load_config(download.DEFAULT_CONFIG)
    return ids, {c.handle.lower() for c in channels}


def search_queries(cfg: dict, only: list[str] | None) -> dict[str, dict]:
    """Run each query once (cached on disk). Returns {video_id: entry + 'queries' + 'languages'}."""
    cache = llm.read_json(SEARCH_CACHE) or {}
    found: dict[str, dict] = {}
    n = cfg["settings"]["results_per_query"]
    options = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with YoutubeDL(options) as ydl:
        for query in cfg["queries"]:
            if only and query["name"] not in only:
                continue
            key = f"{n}|{query['text']}"
            if key not in cache:
                try:
                    info = ydl.extract_info(f"ytsearch{n}:{query['text']}", download=False)
                except Exception as exc:  # noqa: BLE001
                    log.error("search %s failed: %s", query["name"], exc)
                    continue
                cache[key] = [{k: e.get(k) for k in ("id", "title", "channel", "channel_id", "uploader_id",
                                                     "duration", "view_count", "description")}
                              for e in info.get("entries") or [] if e and e.get("id")]
                llm.write_json(SEARCH_CACHE, cache)
                time.sleep(random.uniform(1, 3))
            log.info("[discover:%s] %d results", query["name"], len(cache[key]))
            for entry in cache[key]:
                row = found.setdefault(entry["id"], {**entry, "queries": [], "languages": query["languages"]})
                row["queries"].append(query["name"])
    return found


def apply_filters(found: dict[str, dict]) -> tuple[list[dict], dict]:
    """Drop what we already have and what Layer 0 (profile `discover`) rejects."""
    have = known_video_ids()
    channel_ids, handles = known_channels()
    fresh, own_channel = [], 0
    for entry in found.values():
        if entry["id"] in have:
            continue
        if entry.get("channel_id") in channel_ids or (entry.get("uploader_id") or "").lower() in handles:
            own_channel += 1  # a configured channel: the regular download already covers it
            continue
        fresh.append(entry)
    result = filters.apply_layer0(fresh, filters.load_layer0("discover"))
    filters.save_layer0("discover", result)
    for line in filters.format_report("discover", result["stats"]).splitlines():
        log.info(line)
    result["stats"]["found"] = len(found)
    result["stats"]["already_have"] = len([e for e in found.values() if e["id"] in have])
    result["stats"]["configured_channel"] = own_channel
    return result["kept"], result["stats"]


# ------------------------------------------------------------------------------ stage A


def screen_text(entry: dict, cfg: dict) -> str:
    snippet = (entry.get("description") or "").strip()[: cfg["description_chars"]] or "(not available)"
    minutes = round((entry.get("duration") or 0) / 60)
    return (f"Title: {entry.get('title')}\nChannel: {entry.get('channel')}\n"
            f"Duration: {minutes} min | Views: {entry.get('view_count')}\n\n"
            f"Description snippet:\n{snippet}")


def screen_params(entry: dict, cfg: dict) -> dict:
    return {
        "model": cfg["model"], "max_tokens": cfg["max_tokens"], "system": SCREEN_PROMPT,
        "messages": [{"role": "user", "content": screen_text(entry, cfg)}],
        "output_config": {"effort": cfg["effort"], "format": {"type": "json_schema", "schema": SCREEN_SCHEMA}},
        "thinking": {"type": "disabled"},
    }


def load_screen(video_id: str, cfg: dict) -> dict | None:
    entry = llm.read_json(SCREEN_DIR / f"{video_id}.json")
    if entry and entry.get("model") == cfg["model"] and entry.get("prompt_version") == cfg["prompt_version"]:
        return entry
    return None


def passes_screen(entry: dict, cfg: dict) -> bool:
    return entry["result"]["relevance_score"] >= cfg["min_score"]


def run_screen(kept: list[dict], cfg: dict, sync: bool, yes: bool, limit: int | None) -> int:
    pending = [e for e in kept if load_screen(e["id"], cfg) is None]
    if limit:
        pending = pending[:limit]
    print(f"Stage A: {len(kept)} candidates, {len(kept) - len(pending)} already screened, {len(pending)} to screen.")
    if not pending:
        return 0
    requests = {e["id"]: screen_params(e, cfg) for e in pending}
    ids = list(requests)[: cfg["estimate_sample_size"]]
    try:
        client = llm.get_client()
        counts = [llm.count_input_tokens(client, cfg["model"], SCREEN_PROMPT,
                                         requests[i]["messages"][0]["content"]) for i in ids]
        method = f"count_tokens on {len(ids)} of {len(requests)}, extrapolated"
    except Exception as exc:  # noqa: BLE001
        counts = [llm.rough_token_estimate(SCREEN_PROMPT, requests[i]["messages"][0]["content"]) for i in ids]
        method = f"ROUGH heuristic, count_tokens unavailable: {type(exc).__name__}"
    batch = not sync
    tokens_in = sum(counts) / len(counts) * len(requests)
    tokens_out = cfg["est_output_tokens"] * len(requests)
    cost = llm.cost_usd(cfg["model"], tokens_in, tokens_out, batch)
    print(f"Estimate ({cfg['model']}, {'batch' if batch else 'sync'}): ~{tokens_in:,.0f} in + ~{tokens_out:,.0f} out "
          f"[{method}]\nEstimated cost: " + (f"US$ {cost:.4f}" if cost is not None else "unavailable (pricing placeholders)"))
    if not yes:
        print("Estimate only. Re-run with --yes to execute the paid calls.")
        return 0
    try:
        llm.require_verified_pricing(cfg["model"], batch)
    except llm.PricingNotVerified as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    client = llm.get_client()
    by_id = {e["id"]: e for e in pending}
    saved = invalid = 0

    def store(video_id: str, message, via_batch: bool) -> None:
        nonlocal saved, invalid
        import json
        try:
            data = json.loads(llm.message_text(message))
            result = {"relevance_score": int(data["relevance_score"]), "reason": str(data["reason"])}
            assert 0 <= result["relevance_score"] <= 10
        except (ValueError, KeyError, TypeError, AssertionError):
            invalid += 1
            return
        entry = by_id[video_id]
        llm.write_json(SCREEN_DIR / f"{video_id}.json", {
            "video_id": video_id, "title": entry.get("title"), "channel": entry.get("channel"),
            "channel_id": entry.get("channel_id"), "uploader_id": entry.get("uploader_id"),
            "model": cfg["model"], "prompt_version": cfg["prompt_version"], "result": result,
            "usage": {"input_tokens": message.usage.input_tokens, "output_tokens": message.usage.output_tokens},
            "via_batch": via_batch, "ts": llm.now_iso(),
            "queries": entry.get("queries"), "languages": entry.get("languages"),
        })
        saved += 1

    if sync:
        for video_id, params in requests.items():
            try:
                store(video_id, client.messages.create(**params), False)
            except Exception as exc:  # noqa: BLE001
                print(f"  {video_id}: API error {type(exc).__name__}: {exc}", file=sys.stderr)
    else:
        batch_id = llm.submit_or_resume_batch(client, "discover_screen", requests)
        llm.wait_for_batch(client, batch_id)
        for video_id, message, error in llm.iter_batch_results(client, batch_id):
            if video_id in by_id and message is not None:
                store(video_id, message, True)
    print(f"Stage A done: saved={saved} invalid_output={invalid} (rerun to retry missing ones)")
    return 0


# ------------------------------------------------------------------------------ stage B


def survivors(kept: list[dict], cfg: dict) -> list[dict]:
    out = []
    for entry in kept:
        screened = load_screen(entry["id"], cfg)
        if screened and passes_screen(screened, cfg):
            out.append({**entry, "screen_score": screened["result"]["relevance_score"]})
    return sorted(out, key=lambda e: -e["screen_score"])


def run_download(kept: list[dict], cfg: dict, limit: int | None, cookies: str | None) -> None:
    settings, _ = download.load_config(download.DEFAULT_CONFIG)
    layer0 = filters.load_layer0("discover")
    layer0.views_percentile = 0
    name = cfg["download_channel_name"]
    out_dir = download.TRANSCRIPTS_DIR / name
    todo = [e for e in survivors(kept, cfg) if not (out_dir / f"{e['id']}.json").exists()]
    todo = todo[:limit] if limit else todo
    permanent = download.load_permanent_failures()
    print(f"Stage B: downloading {len(todo)} videos into data/transcripts/{name}/")
    done = failed = skipped = 0
    with YoutubeDL(download.build_ydl_options(cookies)) as ydl:
        for entry in todo:
            if entry["id"] in permanent:
                continue
            channel = download.ChannelConfig(name=name, handle="", max_videos=None,
                                             min_date=download.subtract_months(date.today(), settings.default_max_age_months),
                                             languages=entry.get("languages") or ["en"])
            time.sleep(random.uniform(settings.sleep_min, settings.sleep_max))
            try:
                download.process_video(ydl, entry, channel, settings, layer0, out_dir)
                done += 1
            except download.SkipVideo as exc:
                skipped += 1
                log.info("[discover] skipped %s: %s", entry["id"], exc)
            except download.PermanentError as exc:
                failed += 1
                download.record_failure(name, entry, str(exc), permanent=True)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.error("[discover] FAILED %s: %s", entry["id"], exc)
                download.record_failure(name, entry, str(exc), permanent=False)
    print(f"Stage B done: downloaded={done} skipped={skipped} failed={failed}")


# ------------------------------------------------------------------------------ report


def write_channel_candidates(cfg: dict) -> Path:
    """Channels behind the screened results. Review by hand: nothing is added automatically."""
    rows: dict[str, dict] = defaultdict(lambda: {"screened": 0, "passed": 0, "scores": [], "handle": "", "name": ""})
    for path in SCREEN_DIR.glob("*.json"):
        entry = llm.read_json(path)
        if not entry or entry.get("prompt_version") != cfg["prompt_version"]:
            continue
        key = entry.get("channel_id") or entry.get("uploader_id") or entry.get("channel")
        row = rows[key]
        row.update(handle=entry.get("uploader_id") or "", name=entry.get("channel") or "", channel_id=key)
        row["screened"] += 1
        row["scores"].append(entry["result"]["relevance_score"])
        row["passed"] += int(entry["result"]["relevance_score"] >= cfg["min_score"])
        row["languages"] = ",".join(entry.get("languages") or [])
    ordered = sorted(rows.values(), key=lambda r: (-r["passed"], -sum(r["scores"]) / len(r["scores"])))
    CANDIDATES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with CANDIDATES_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["channel", "handle", "channel_id", "languages", "videos_screened", "videos_passed", "avg_score"])
        for r in ordered:
            writer.writerow([r["name"], r["handle"], r["channel_id"], r.get("languages", ""), r["screened"], r["passed"],
                             round(sum(r["scores"]) / len(r["scores"]), 1)])
    return CANDIDATES_CSV


# ------------------------------------------------------------------------------ entry


def run(stage: str, only: list[str] | None, limit: int | None, sync: bool, yes: bool, cookies: str | None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    screen_cfg = cfg["settings"]["screen"] | {"download_channel_name": cfg["settings"]["download_channel_name"]}

    found = search_queries(cfg, only)
    kept, stats = apply_filters(found)
    print(f"Search: {stats['found']} unique results | already have {stats['already_have']} | "
          f"from configured channels {stats['configured_channel']} | after Layer 0: {len(kept)}")
    if stage == "search":
        return 0
    if stage == "report":
        print(f"Channel candidates written to {write_channel_candidates(screen_cfg).relative_to(ROOT)}")
        return 0

    if stage in ("all", "screen"):
        code = run_screen(kept, screen_cfg, sync, yes, limit)
        if code or not yes:
            return code
    if stage in ("all", "download"):
        scored = survivors(kept, screen_cfg)
        print(f"Stage A survivors (score >= {screen_cfg['min_score']}): {len(scored)}")
        if stage == "download" or yes:
            run_download(kept, screen_cfg, limit, cookies)
    path = write_channel_candidates(screen_cfg)
    print(f"Channel candidates written to {path.relative_to(ROOT)} (review by hand; nothing was added to channels.yaml)")
    return 0
