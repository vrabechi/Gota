#!/usr/bin/env python
"""Stage 1: download English transcripts for the channels in config/channels.yaml.

Output: data/transcripts/<channel>/<video_id>.json
Resumable: videos already saved (or permanently failed) are skipped on re-run.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

import yaml
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)

from cleaning import clean_plain, clean_vtt

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "channels.yaml"
TRANSCRIPTS_DIR = ROOT / "data" / "transcripts"
FAILURES_FILE = ROOT / "data" / "failures.jsonl"
LOG_FILE = ROOT / "data" / "logs" / "download.log"

log = logging.getLogger("download")
T = TypeVar("T")

# Substrings of yt-dlp errors that will never succeed on retry.
PERMANENT_ERROR_MARKERS = (
    "private video",
    "video unavailable",
    "members-only",
    "members only",
    "has been removed",
    "account associated with this video has been terminated",
    "this video is not available",
)


# --------------------------------------------------------------------------- errors


class SkipVideo(Exception):
    """Video intentionally ignored (short, live, too old...). Not counted as a failure."""


class PermanentError(Exception):
    """Failure that retrying will not fix (no captions, private video...)."""


# ------------------------------------------------------------------------- settings


@dataclass
class Settings:
    sleep_min: float = 2.0
    sleep_max: float = 6.0
    max_retries: int = 3
    backoff_base: float = 5.0
    min_duration_seconds: int = 90
    scan_limit: int = 150
    default_max_age_months: int = 24


@dataclass
class ChannelConfig:
    name: str
    handle: str
    max_videos: int
    min_date: date
    title_keywords: list[str] = field(default_factory=list)


def subtract_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last_day))


def parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def load_config(path: Path) -> tuple[Settings, list[ChannelConfig]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = Settings(**(raw.get("settings") or {}))
    channels: list[ChannelConfig] = []
    for item in raw.get("channels") or []:
        if item.get("min_date"):
            min_date = parse_date(item["min_date"])
        else:
            months = item.get("max_age_months", settings.default_max_age_months)
            min_date = subtract_months(date.today(), months)
        channels.append(
            ChannelConfig(
                name=item["name"],
                handle=item["handle"],
                max_videos=int(item.get("max_videos", 50)),
                min_date=min_date,
                title_keywords=[k.lower() for k in item.get("title_keywords") or []],
            )
        )
    return settings, channels


# ------------------------------------------------------------------------ utilities


def with_retries(fn: Callable[[], T], settings: Settings, what: str) -> T:
    """Run fn with exponential backoff + jitter. PermanentError/SkipVideo are not retried."""
    for attempt in range(1, settings.max_retries + 1):
        try:
            return fn()
        except (PermanentError, SkipVideo):
            raise
        except Exception as exc:  # noqa: BLE001 - network layers raise many types
            if attempt == settings.max_retries:
                raise
            delay = settings.backoff_base * 2 ** (attempt - 1) + random.uniform(0, settings.backoff_base)
            log.warning("%s failed (attempt %d/%d): %s - retrying in %.0fs",
                        what, attempt, settings.max_retries, exc, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")


def channel_videos_url(handle: str) -> str:
    if handle.startswith("http"):
        return handle if handle.rstrip("/").endswith("/videos") else handle.rstrip("/") + "/videos"
    return f"https://www.youtube.com/{handle}/videos"


def build_ydl_options(cookies_from_browser: str | None, **extra: object) -> dict:
    options: dict = {"quiet": True, "no_warnings": True, "skip_download": True, **extra}
    if cookies_from_browser:
        options["cookiesfrombrowser"] = (cookies_from_browser,)
    return options


def load_permanent_failures() -> set[str]:
    if not FAILURES_FILE.exists():
        return set()
    ids: set[str] = set()
    for line in FAILURES_FILE.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("permanent"):
            ids.add(record["video_id"])
    return ids


def record_failure(channel: str, entry: dict, reason: str, permanent: bool) -> None:
    FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel": channel,
        "video_id": entry["id"],
        "title": entry.get("title"),
        "reason": reason,
        "permanent": permanent,
    }
    with FAILURES_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# -------------------------------------------------------------------------- listing


def list_channel_candidates(channel: ChannelConfig, settings: Settings, cookies: str | None) -> list[dict]:
    """Flat-list the newest `scan_limit` videos, then order them by view count (desc).

    The /videos tab excludes Shorts. The flat listing has no upload date, so the date
    filter is applied later, when each candidate's full metadata is fetched.
    """
    options = build_ydl_options(cookies, extract_flat="in_playlist", playlistend=settings.scan_limit)
    with YoutubeDL(options) as ydl:
        info = with_retries(
            lambda: ydl.extract_info(channel_videos_url(channel.handle), download=False),
            settings, f"list {channel.name}",
        )
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    log.info("[%s] listed %d videos (scan_limit=%d)", channel.name, len(entries), settings.scan_limit)

    candidates = []
    for entry in entries:
        title = (entry.get("title") or "").lower()
        if channel.title_keywords and not any(k in title for k in channel.title_keywords):
            continue
        duration = entry.get("duration")
        if duration is not None and duration < settings.min_duration_seconds:
            continue
        candidates.append(entry)
    candidates.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
    log.info("[%s] %d candidates after title/duration filters", channel.name, len(candidates))
    return candidates


# ------------------------------------------------------------------------ transcript


def pick_caption(info: dict) -> tuple[str, str, str] | None:
    """Return (source, language, vtt_url) preferring manual English captions."""
    video_language = (info.get("language") or "").lower()
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    manual_langs = [l for l in manual if l == "en" or l.startswith("en-")]
    # Plain "en" in automatic captions can be a machine translation; "en-orig" is the original speech.
    auto_langs = [l for l in ("en-orig", "en") if l in auto]
    if "en-orig" not in auto and video_language and not video_language.startswith("en"):
        auto_langs = []

    for source, pool, languages in (("manual", manual, manual_langs), ("auto", auto, auto_langs)):
        for language in languages:
            for fmt in pool[language]:
                if fmt.get("ext") == "vtt" and fmt.get("url"):
                    return source, language, fmt["url"]
    return None


def fetch_transcript_via_api(video_id: str) -> tuple[str, str]:
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-US", "en-GB"])
    except (NoTranscriptFound, TranscriptsDisabled) as exc:
        raise PermanentError("no English captions (transcript-api)") from exc
    text = clean_plain(" ".join(snippet.text for snippet in fetched))
    return text, "transcript_api_auto" if fetched.is_generated else "transcript_api_manual"


def get_transcript(ydl: YoutubeDL, info: dict, settings: Settings) -> tuple[str, str, str | None]:
    """Return (text, caption_source, caption_language). yt-dlp first, transcript-api as fallback."""
    caption = pick_caption(info)
    if caption:
        source, language, url = caption
        try:
            raw = with_retries(lambda: ydl.urlopen(url).read().decode("utf-8", "replace"),
                               settings, f"captions {info['id']}")
            text = clean_vtt(raw)
            if text:
                return text, source, language
            log.warning("[%s] yt-dlp caption was empty, trying transcript-api", info["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] yt-dlp caption download failed (%s), trying transcript-api", info["id"], exc)

    text, source = with_retries(lambda: fetch_transcript_via_api(info["id"]),
                                settings, f"transcript-api {info['id']}")
    if not text:
        raise PermanentError("empty transcript")
    return text, source, None


# ---------------------------------------------------------------------- per video


def process_video(ydl: YoutubeDL, entry: dict, channel: ChannelConfig, settings: Settings, out_dir: Path) -> None:
    video_id = entry["id"]

    def fetch_info() -> dict:
        try:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        except DownloadError as exc:
            message = str(exc).lower()
            if any(marker in message for marker in PERMANENT_ERROR_MARKERS):
                raise PermanentError(f"unavailable: {exc}") from exc
            raise

    info = with_retries(fetch_info, settings, f"metadata {video_id}")

    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise SkipVideo("live/upcoming")
    duration = info.get("duration")
    if duration is not None and duration < settings.min_duration_seconds:
        raise SkipVideo("too short (Short?)")
    upload_date = datetime.strptime(info["upload_date"], "%Y%m%d").date() if info.get("upload_date") else None
    if upload_date is None:
        raise SkipVideo("no upload date")
    if upload_date < channel.min_date:
        raise SkipVideo(f"older than {channel.min_date}")

    text, caption_source, caption_language = get_transcript(ydl, info, settings)

    record = {
        "video_id": video_id,
        "title": info.get("title"),
        "channel": info.get("channel") or channel.name,
        "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
        "upload_date": upload_date.isoformat(),
        "view_count": info.get("view_count"),
        "duration": duration,
        "transcript_text": text,
        "caption_source": caption_source,
        "caption_language": caption_language,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{video_id}.json"
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)  # atomic: a crash never leaves a half-written transcript
    log.info("[%s] saved %s (%s, %d chars) %s", channel.name, video_id, caption_source, len(text), record["title"])


# ------------------------------------------------------------------------- channel


@dataclass
class ChannelStats:
    downloaded: int = 0
    already_present: int = 0
    known_failures_skipped: int = 0
    skipped: Counter = field(default_factory=Counter)
    failed: Counter = field(default_factory=Counter)


def process_channel(channel: ChannelConfig, settings: Settings, cookies: str | None,
                    retry_failed: bool, dry_run: bool) -> ChannelStats:
    stats = ChannelStats()
    candidates = list_channel_candidates(channel, settings, cookies)
    if dry_run:
        for entry in candidates[: channel.max_videos]:
            print(f"  {entry.get('view_count') or '?':>10} views  {entry['id']}  {entry.get('title')}")
        return stats

    out_dir = TRANSCRIPTS_DIR / channel.name
    permanent_failures = set() if retry_failed else load_permanent_failures()
    saved = 0

    with YoutubeDL(build_ydl_options(cookies)) as ydl:
        for entry in candidates:
            if saved >= channel.max_videos:
                break
            video_id = entry["id"]
            if (out_dir / f"{video_id}.json").exists():
                stats.already_present += 1
                saved += 1
                continue
            if video_id in permanent_failures:
                stats.known_failures_skipped += 1
                continue

            time.sleep(random.uniform(settings.sleep_min, settings.sleep_max))
            try:
                process_video(ydl, entry, channel, settings, out_dir)
                stats.downloaded += 1
                saved += 1
            except SkipVideo as exc:
                stats.skipped[str(exc)] += 1
                log.info("[%s] skipped %s: %s", channel.name, video_id, exc)
            except PermanentError as exc:
                stats.failed[str(exc)[:80]] += 1
                log.error("[%s] FAILED %s (permanent): %s", channel.name, video_id, exc)
                record_failure(channel.name, entry, str(exc), permanent=True)
            except Exception as exc:  # noqa: BLE001
                stats.failed[str(exc)[:80]] += 1
                log.error("[%s] FAILED %s (after retries): %s", channel.name, video_id, exc)
                record_failure(channel.name, entry, str(exc), permanent=False)
                if "confirm you" in str(exc).lower() and "bot" in str(exc).lower():
                    log.error("YouTube is blocking requests. Re-run with --cookies-from-browser <browser>.")
    return stats


def print_summary(results: dict[str, ChannelStats]) -> None:
    print("\n=== Summary ===")
    for name, stats in results.items():
        total_failed = sum(stats.failed.values())
        print(f"{name}: downloaded={stats.downloaded} already_present={stats.already_present} "
              f"skipped={sum(stats.skipped.values())} failed={total_failed} "
              f"known_failures_skipped={stats.known_failures_skipped}")
        for reason, count in stats.skipped.items():
            print(f"    skipped x{count}: {reason}")
        for reason, count in stats.failed.items():
            print(f"    failed  x{count}: {reason}")


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--channel", action="append", help="channel name from the config (repeatable); default: all")
    parser.add_argument("--max-videos", type=int, help="override max_videos for every selected channel")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER", help="e.g. chrome, firefox, edge")
    parser.add_argument("--retry-failed", action="store_true", help="retry videos that failed permanently before")
    parser.add_argument("--dry-run", action="store_true", help="only list the videos that would be downloaded")
    args = parser.parse_args()

    setup_logging()
    settings, channels = load_config(args.config)
    if args.channel:
        unknown = set(args.channel) - {c.name for c in channels}
        if unknown:
            parser.error(f"unknown channel(s) {sorted(unknown)}; available: {[c.name for c in channels]}")
        channels = [c for c in channels if c.name in args.channel]
    if args.max_videos:
        for channel in channels:
            channel.max_videos = args.max_videos

    results: dict[str, ChannelStats] = {}
    for channel in channels:
        log.info("=== %s (%s) | max_videos=%d min_date=%s ===",
                 channel.name, channel.handle, channel.max_videos, channel.min_date)
        results[channel.name] = process_channel(
            channel, settings, args.cookies_from_browser, args.retry_failed, args.dry_run)
    if not args.dry_run:
        print_summary(results)


if __name__ == "__main__":
    main()
