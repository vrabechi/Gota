#!/usr/bin/env python
"""Funnel commands. Download stays in download.py.

  python src/cli.py layer0  [--channel NAME ...]            Layer 0 report (free, no downloads)
  python src/cli.py collect --source hackernews [--limit N] [--dry-run]
  python src/cli.py triage  [--source S ...] [--channel NAME ...] [--limit N] [--sync] [--yes]
  python src/cli.py funnel  [--details]                     in/out per layer + costs
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import documents
import download
import filters
import hackernews
import llm
import triage
import yaml

ROOT = Path(__file__).resolve().parent.parent


def cmd_layer0(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.WARNING)
    settings, channels = download.load_config(args.config)
    if args.channel:
        channels = [c for c in channels if c.name in args.channel]
    layer0 = filters.load_layer0("default")
    for channel in channels:
        kept = download.list_channel_candidates(channel, settings, layer0, args.cookies_from_browser)
        saved = llm.read_json(filters.FUNNEL_DIR / f"layer0_{channel.name}.json")
        print(filters.format_report(channel.name, saved["stats"]))
        if args.show_kept:
            for entry in kept[:args.show_kept]:
                print(f"    keep {entry.get('prefilter_score'):+.0f} {entry.get('view_count') or 0:>9,}v  {entry.get('title')}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sources_cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    if args.source not in sources_cfg:
        print(f"Unknown source {args.source!r}. Configured: {sorted(sources_cfg)}", file=sys.stderr)
        return 2
    result = hackernews.collect(sources_cfg[args.source], args.limit, args.dry_run, args.query)
    if args.dry_run:
        for entry in result["candidates"][:args.show]:
            print(f"  {entry['score']:>5} pts {entry['num_comments']:>4} comments  {entry['id']}  {entry['title']}")
        print(f"{len(result['candidates'])} candidates would be downloaded.")
    else:
        print(f"collect {args.source}: downloaded={result['downloaded']} "
              f"already_present={result['already_present']} failed={result['failed']}")
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    return triage.run(args.channel, args.limit, args.sync, args.yes, args.source)


def cmd_funnel(args: argparse.Namespace) -> int:
    cfg = triage.load_cfg()
    entered = kept = 0
    print("=== Funnel ===")
    for path in sorted(filters.FUNNEL_DIR.glob("layer0_*.json")):
        data = llm.read_json(path)
        entered += data["stats"]["entered"]
        kept += data["stats"]["kept"]
        print(f"  Layer 0  {data['label']:<20} {data['stats']['entered']:>4} in -> {data['stats']['kept']:>4} out")
    print(f"  Layer 0  {'TOTAL':<20} {entered:>4} in -> {kept:>4} out   cost: US$ 0 (metadata only)")

    records = documents.load_all()
    by_source = Counter(r["source"] for r in records)
    print(f"  Download {'documents on disk':<20} {len(records):>4}   (" +
          ", ".join(f"{s}: {n}" for s, n in sorted(by_source.items())) + ")")

    cached = [e for r in records if (e := triage.load_cached(r, cfg))]
    passed = [e for e in cached if triage.passes_layer1(e, cfg)]
    print(f"  Layer 1  {'triaged (cached)':<20} {len(records):>4} in -> {len(cached):>4} triaged, "
          f"{len(passed)} pass (concrete_business={cfg['require_concrete_business']}, "
          f"score>={cfg['min_relevance_score']})")
    for source in sorted(by_source):
        source_cached = [e for e in cached if e.get("source", "youtube") == source]
        source_passed = [e for e in source_cached if triage.passes_layer1(e, cfg)]
        print(f"           {source:<20} {by_source[source]:>4} in -> {len(source_cached):>4} triaged, "
              f"{len(source_passed)} pass")
    tokens_in = sum(e["usage"]["input_tokens"] for e in cached)
    tokens_out = sum(e["usage"]["output_tokens"] for e in cached)
    pricing = llm.load_pricing()
    cost = 0.0
    priced = True
    for e in cached:
        c = llm.cost_usd(cfg["model"], e["usage"]["input_tokens"], e["usage"]["output_tokens"],
                         e["via_batch"], pricing)
        if c is None:
            priced = False
            break
        cost += c
    if not cached:
        print("           actual usage: none yet")
    else:
        print(f"           actual usage: {tokens_in:,} input + {tokens_out:,} output tokens; cost: "
              + (f"US$ {cost:.4f}" if priced else "unavailable until pricing.yaml is verified"))
    print(f"  Layer 2  {'approved for extraction':<20} {len(passed):>4}   (not implemented yet; estimate comes in step 3)")

    if cached:
        histogram = Counter(e["result"]["relevance_score"] for e in cached)
        print("\nScore distribution: " + "  ".join(f"{s}:{histogram[s]}" for s in sorted(histogram)))
    if args.details:
        print("\nscore conc nums  PASS  title -- reason")
        for e in sorted(cached, key=lambda e: -e["result"]["relevance_score"]):
            r = e["result"]
            print(f"{r['relevance_score']:>5} "
                  f"{'Y' if r['has_concrete_business'] else 'n':>4} {'Y' if r['has_numbers'] else 'n':>4}  "
                  f"{'PASS' if triage.passes_layer1(e, cfg) else 'drop'}  {e['title']} -- {r['reason']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p0 = sub.add_parser("layer0", help="run Layer 0 on the fast channel listing and print the report")
    p0.add_argument("--config", type=Path, default=download.DEFAULT_CONFIG)
    p0.add_argument("--channel", action="append")
    p0.add_argument("--cookies-from-browser", metavar="BROWSER")
    p0.add_argument("--show-kept", type=int, default=0, metavar="N", help="print the top N kept titles")
    p0.set_defaults(func=cmd_layer0)

    pc = sub.add_parser("collect", help="download documents from a non-YouTube source (see config/sources.yaml)")
    pc.add_argument("--source", required=True, help="hackernews")
    pc.add_argument("--query", action="append", help="run only this named query from sources.yaml (repeatable)")
    pc.add_argument("--limit", type=int, help="keep only the N highest-engagement candidates")
    pc.add_argument("--dry-run", action="store_true", help="search + Layer 0 only, download nothing")
    pc.add_argument("--show", type=int, default=15, help="with --dry-run, how many candidates to print")
    pc.set_defaults(func=cmd_collect)

    p1 = sub.add_parser("triage", help="Layer 1: Haiku triage of downloaded transcripts (paid; estimate first)")
    p1.add_argument("--channel", action="append", help="YouTube channel slug (repeatable)")
    p1.add_argument("--source", action="append", help="youtube, hackernews... (repeatable); default: all")
    p1.add_argument("--limit", type=int)
    p1.add_argument("--sync", action="store_true", help="plain API calls instead of Batch (no discount, instant)")
    p1.add_argument("--yes", action="store_true", help="actually run the paid calls after seeing the estimate")
    p1.set_defaults(func=cmd_triage)

    pf = sub.add_parser("funnel", help="report entries/exits and cost per layer")
    pf.add_argument("--details", action="store_true", help="list every triaged video")
    pf.set_defaults(func=cmd_funnel)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
