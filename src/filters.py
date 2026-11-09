"""Layer 0 of the funnel: metadata-only filters (no LLM, no cost)."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FILTERS_FILE = ROOT / "config" / "filters.yaml"
FUNNEL_DIR = ROOT / "data" / "funnel"


@dataclass
class TitleRule:
    pattern: str
    weight: float
    regex: re.Pattern


@dataclass
class Layer0Config:
    min_duration_seconds: int | None   # None = source has no duration (skip the duration stage)
    max_duration_seconds: int | None
    views_percentile: float            # 0 disables the percentile stage
    min_group_size_for_percentile: int
    views_per_day: bool
    prefilter_cutoff: float
    metric_field: str = "view_count"   # engagement field used by the percentile stage
    title_rules_enabled: bool = True   # False skips the title stage (rules stay in the YAML)
    boost: list[TitleRule] = field(default_factory=list)
    penalize: list[TitleRule] = field(default_factory=list)


def load_yaml(path: Path = FILTERS_FILE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _compile_rules(items: list[dict] | None) -> list[TitleRule]:
    return [TitleRule(i["pattern"], float(i.get("weight", 1)), re.compile(i["pattern"], re.IGNORECASE))
            for i in items or []]


def load_layer0(profile: str = "default", path: Path = FILTERS_FILE) -> Layer0Config:
    layer0 = load_yaml(path)["layer0"]
    merged = dict(layer0["default"])
    if profile != "default":
        merged.update(layer0.get(profile) or {})
    optional_int = lambda v: None if v is None else int(v)  # noqa: E731
    return Layer0Config(
        min_duration_seconds=optional_int(merged.get("min_duration_seconds")),
        max_duration_seconds=optional_int(merged.get("max_duration_seconds")),
        views_percentile=float(merged["views_percentile"]),
        min_group_size_for_percentile=int(merged["min_group_size_for_percentile"]),
        views_per_day=bool(merged["views_per_day"]),
        prefilter_cutoff=float(merged["prefilter_cutoff"]),
        metric_field=merged.get("metric_field", "view_count"),
        title_rules_enabled=bool(merged.get("title_rules", True)),
        boost=_compile_rules(merged.get("boost")),
        penalize=_compile_rules(merged.get("penalize")),
    )


def _parse_upload_date(value: object) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError:
        return date.fromisoformat(str(value))


def _views_metric(entry: dict, cfg: Layer0Config, stats: Counter) -> float:
    views = entry.get(cfg.metric_field) or 0
    if not cfg.views_per_day:
        return float(views)
    uploaded = _parse_upload_date(entry.get("upload_date"))
    if uploaded is None:
        stats["views_per_day_fallback_to_raw_views"] += 1
        return float(views)
    return views / max(1, (date.today() - uploaded).days)


def apply_layer0(entries: list[dict], cfg: Layer0Config) -> dict:
    """Return {"kept": [...], "dropped": [...], "stats": {...}}.

    Stages run in order and each video is attributed to the FIRST stage that drops it:
    duration -> views percentile (per channel) -> title score.
    """
    stats: dict = {"entered": len(entries), "dropped_by": Counter(), "notes": Counter()}
    dropped: list[dict] = []

    def drop(entry: dict, reason: str) -> None:
        stats["dropped_by"][reason] += 1
        dropped.append({**entry, "drop_reason": reason})

    # Stage 1: duration (also removes Shorts and live/upcoming streams, which have no duration).
    after_duration = []
    for entry in entries:
        duration = entry.get("duration")
        if cfg.min_duration_seconds is None and cfg.max_duration_seconds is None:
            after_duration.append(entry)  # text sources have no duration
        elif duration is None or entry.get("live_status") in ("is_live", "is_upcoming"):
            drop(entry, "duration:missing_or_live")
        elif cfg.min_duration_seconds is not None and duration < cfg.min_duration_seconds:
            drop(entry, "duration:too_short")
        elif cfg.max_duration_seconds is not None and duration > cfg.max_duration_seconds:
            drop(entry, "duration:too_long")
        else:
            after_duration.append(entry)

    # Stage 2: engagement percentile within each channel (skipped when views_percentile is 0).
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in after_duration:
        groups[entry.get("channel_id") or entry.get("channel") or "_all"].append(entry)
    after_views = []
    for members in groups.values():
        if cfg.views_percentile <= 0:
            after_views.extend(members)
            continue
        if len(members) < cfg.min_group_size_for_percentile:
            stats["notes"]["views_percentile_skipped_small_group"] += len(members)
            after_views.extend(members)
            continue
        metrics = {id(e): _views_metric(e, cfg, stats["notes"]) for e in members}
        ordered = sorted(metrics.values())
        threshold = ordered[min(len(ordered) - 1, int(len(ordered) * cfg.views_percentile / 100))]
        for entry in members:
            if metrics[id(entry)] >= threshold:
                after_views.append(entry)
            else:
                drop(entry, f"views:below_P{cfg.views_percentile:g}")

    # Stage 3: title rules -> prefilter_score.
    rule_stats: dict[str, Counter] = {}
    kept = []
    for entry in after_views:
        if not cfg.title_rules_enabled:
            kept.append({**entry, "prefilter_score": 0.0})
            continue
        title = entry.get("title") or ""
        matched_boost = [r for r in cfg.boost if r.regex.search(title)]
        matched_penalty = [r for r in cfg.penalize if r.regex.search(title)]
        score = sum(r.weight for r in matched_boost) - sum(r.weight for r in matched_penalty)
        survives = score >= cfg.prefilter_cutoff
        for kind, rules in (("boost", matched_boost), ("penalize", matched_penalty)):
            for rule in rules:
                counter = rule_stats.setdefault(f"{kind}: {rule.pattern}", Counter())
                counter["matched"] += 1
                counter["matched_and_kept" if survives else "matched_and_dropped"] += 1
        if survives:
            kept.append({**entry, "prefilter_score": score})
        else:
            drop({**entry, "prefilter_score": score}, "title:prefilter_score_below_cutoff")

    stats["kept"] = len(kept)
    stats["rules"] = {name: dict(c) for name, c in rule_stats.items()}
    stats["dropped_by"] = dict(stats["dropped_by"])
    stats["notes"] = dict(stats["notes"])
    return {"kept": kept, "dropped": dropped, "stats": stats}


def format_report(label: str, stats: dict) -> str:
    lines = [f"Layer 0 [{label}]: {stats['entered']} in -> {stats['kept']} out"]
    for reason, count in sorted(stats["dropped_by"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    dropped {count:>4}  {reason}")
    for name, counts in stats["rules"].items():
        lines.append(f"    rule {name}: matched={counts.get('matched', 0)} "
                     f"dropped={counts.get('matched_and_dropped', 0)} kept={counts.get('matched_and_kept', 0)}")
    for note, count in stats["notes"].items():
        lines.append(f"    note: {note} = {count}")
    return "\n".join(lines)


def save_layer0(label: str, result: dict) -> Path:
    """Persist the stats plus kept/dropped ids so the funnel report can read them later."""
    FUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    path = FUNNEL_DIR / f"layer0_{label}.json"
    brief = lambda e: {"id": e["id"], "title": e.get("title"), "views": e.get("view_count") or e.get("score"),
                       "duration": e.get("duration"), "prefilter_score": e.get("prefilter_score"),
                       "drop_reason": e.get("drop_reason")}
    payload = {
        "label": label,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "stats": result["stats"],
        "kept": [brief(e) for e in result["kept"]],
        "dropped": [brief(e) for e in result["dropped"]],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
