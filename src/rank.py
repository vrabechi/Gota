"""Rank the deduped businesses with config/scoring.yaml. Free (no API calls).

Reads data/dedupe/clusters.json + data/extractions/<model>/. Writes data/rank/ranking.json and data/rank/report.md.
Every record is excluded or scored on its own; a cluster takes the score of its best non-excluded record.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

import llm

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "scoring.yaml"
CLUSTERS_FILE = ROOT / "data" / "dedupe" / "clusters.json"
EXTRACTION_DIR = ROOT / "data" / "extractions"
OUT_DIR = ROOT / "data" / "rank"

PERIOD_TO_MONTHS = {"month": 1.0, "year": 12.0}   # 'cumulative', 'one_time', 'unspecified' have no monthly rate


def load_cfg() -> dict:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))


SCALE_WORDS = r"(million|millions|billion|mil|mm|thousand|k|m|b)"
SCALE_FACTOR = {"million": 1e6, "millions": 1e6, "mm": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9,
                "thousand": 1e3, "k": 1e3, "mil": 1e3}


def scaled_value(m: dict) -> float | None:
    """The extractor sometimes reads '$60 million' as 60. Fix it from the quote, or return None when the quote has a
    scale word the value does not reflect and the multiplier cannot be told (the figure is then ignored)."""
    import re
    value, quote = m["value"], (m.get("supporting_quote") or "").lower()
    if value >= 1000 or not re.search(r"\b" + SCALE_WORDS + r"\b|\d\s?[mbk]\b", quote):
        return value
    number = re.escape(f"{value:g}")
    hit = re.search(number + r"(?:\.\d+)?\s*" + SCALE_WORDS + r"\b", quote)
    return value * SCALE_FACTOR[hit.group(1)] if hit else None


def verified_usd(business: dict, kinds: set[str]) -> list[dict]:
    out = []
    for m in business.get("metrics", []):
        if m["kind"] in kinds and m.get("value") is not None and m.get("quote_verified")                 and m.get("unit") == "currency" and m.get("currency") == "USD":
            value = scaled_value(m)
            if value is not None:
                out.append({**m, "value": value})
    return out


def monthly_usd(business: dict, kinds: set[str]) -> float | None:
    """Largest verified USD figure of these kinds, converted to a monthly rate; None when there is none."""
    values = [m["value"] / PERIOD_TO_MONTHS[m["period"]] for m in verified_usd(business, kinds)
              if m.get("period") in PERIOD_TO_MONTHS]
    return max(values) if values else None


def exclusion_reason(b: dict, cfg: dict) -> str | None:
    ex, ops = cfg["exclude"], b["operation"]
    if ops["physical_operations"] in ex["physical_operations"]:
        return f"physical_operations={ops['physical_operations']}"
    if b["stage"] in ex["stages"]:
        return f"stage={b['stage']}"
    if b["prominence"] in ex["prominence"]:
        return f"prominence={b['prominence']}"
    yearly = monthly_usd(b, {"revenue"})
    if yearly is not None and yearly * 12 >= ex["giant"]["revenue_usd_per_year"]:
        return "giant (verified revenue)"
    exits = [m["value"] for m in verified_usd(b, {"exit_price"})]
    if exits and max(exits) >= ex["giant"]["exit_price_usd"]:
        return "giant (verified exit price)"
    return None


def credit_us(b: dict, cfg: dict) -> float:
    table = cfg["us_dependency_credit"]
    n = len(b["operation"]["us_specific_dependencies"])
    return table[min(n, max(table))]


def credit_traction(b: dict, cfg: dict) -> float:
    t = cfg["traction"]
    anything = [m for m in b.get("metrics", [])
                if m["kind"] in {"revenue", "profit", "customers"} and m.get("value") is not None and m.get("quote_verified")]
    if not anything:
        return t["unknown_credit"]
    rate = monthly_usd(b, {"revenue", "profit"})
    if rate is None:
        return t["verified_unusable_credit"]
    low, high = t["band_usd_per_month"]
    return t["verified_in_band_credit"] if low <= rate <= high else t["verified_out_of_band_credit"]


def credit_operable(b: dict, cfg: dict) -> float:
    o, ops = cfg["operable_solo"], b["operation"]
    return (o["automation"][ops["automation_level"]] + o["support_load"][ops["support_load"]]) / 2


def credit_barrier(b: dict, cfg: dict) -> float:
    e = cfg["entry_barrier"]
    capital = max((m["value"] for m in verified_usd(b, {"initial_capital"})), default=None)
    if capital is None:
        cap_credit = e["unknown_capital_credit"]
    else:
        cap_credit = next(c for limit, c in e["capital_usd_credit"] if limit is None or capital <= limit)
    listed = min(len(b["operation"]["entry_barriers"]), e["barriers_listed_max"])
    return (cap_credit + (1 - listed / e["barriers_listed_max"])) / 2


def credit_ceiling(b: dict, cfg: dict) -> float:
    c = cfg["ceiling"]
    base = c["category_credit"].get(b["category"], c["category_credit"]["other"])
    if set(b["monetization_models"]) & set(c["recurring_models"]):
        base += c["recurring_bonus"]
    return min(base, 1.0)


def score(b: dict, cfg: dict) -> dict:
    w = cfg["weights"]
    parts = {
        "us_dependency": w["us_dependency"] * credit_us(b, cfg),
        "traction": w["traction"] * credit_traction(b, cfg),
        "operable_solo": w["operable_solo"] * credit_operable(b, cfg),
        "stage": w["stage"] * cfg["stage_credit"][b["stage"]],
        "entry_barrier": w["entry_barrier"] * credit_barrier(b, cfg),
        "ceiling": w["ceiling"] * credit_ceiling(b, cfg),
    }
    p, ops, penalties = cfg["penalties"], b["operation"], {}
    if ops["revenue_source"] == "sells_courses_about_it":
        penalties["sells_courses_about_it"] = p["sells_courses_about_it"]
    if b.get("single_platform_dependency"):
        penalties["single_platform_dependency"] = p["single_platform_dependency"]
    if ops["regulated_industry"]:
        penalties["regulated_industry"] = p["regulated_industry"]
    if ops["language_dependent"]:
        penalties["language_dependent"] = p["language_dependent"]
    total = sum(parts.values()) - sum(penalties.values())
    return {"score": round(total, 1), "parts": {k: round(v, 1) for k, v in parts.items()}, "penalties": penalties}


def load_business(model: str, rid: str) -> dict | None:
    doc_key, index = rid.rsplit("#", 1)
    entry = llm.read_json(EXTRACTION_DIR / model / f"{doc_key}.json")
    return entry["businesses"][int(index)] if entry else None


def run() -> int:
    cfg = load_cfg()
    data = llm.read_json(CLUSTERS_FILE)
    if not data:
        print("Run `dedupe` first.")
        return 1
    model, records = data["extraction_model"], data["records"]
    ranked, failures, excluded = [], [], Counter()
    for cl in data["clusters"]:
        best, reasons = None, []
        for rid in cl["members"]:
            b = load_business(model, rid)
            if b is None:
                continue
            reason = exclusion_reason(b, cfg)
            if reason:
                reasons.append(reason)
                continue
            s = score(b, cfg)
            if best is None or s["score"] > best["score"]:
                best = {**s, "rid": rid, "business": b}
        channels = cl["channels"]
        base = {"cluster_id": cl["cluster_id"], "mentions": cl["mentions"], "documents": cl["documents"],
                "channels": channels}
        if best:
            b = best["business"]
            ranked.append({**base, "name": b["name"] or cl["name"] or "(unnamed)", "description": b["description"],
                           "category": b["category"], "stage": b["stage"], "score": best["score"],
                           "parts": best["parts"], "penalties": best["penalties"], "rid": best["rid"],
                           "monthly_revenue_usd": monthly_usd(b, {"revenue"}),
                           "us_dependencies": b["operation"]["us_specific_dependencies"],
                           "url": records[best["rid"]]["url"], "doc_title": records[best["rid"]]["doc_title"],
                           "all_sources": [f"{records[m]['doc_title']} | {records[m]['url']}" for m in cl["members"]],
                           "figures": [{"kind": m["kind"], "value": m["value"], "currency": m["currency"],
                                        "period": m["period"], "quote": m["supporting_quote"]}
                                       for m in b["metrics"]
                                       if m["kind"] in ("revenue", "profit", "customers", "initial_capital")
                                       and m.get("value") is not None and m.get("quote_verified")][:5]})
        else:
            excluded[Counter(reasons).most_common(1)[0][0] if reasons else "no data"] += 1
            if any(r == "stage=shut_down" for r in reasons):
                failures.append({**base, "name": cl["name"]})
    ranked.sort(key=lambda r: -r["score"])
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    llm.write_json(OUT_DIR / "ranking.json", {"ranked": ranked, "excluded": dict(excluded), "failures": failures})
    write_report(ranked, excluded, failures, cfg, len(data["clusters"]))
    write_csv(ranked)
    print(f"{len(data['clusters'])} businesses -> {len(ranked)} ranked, {sum(excluded.values())} excluded")
    for reason, n in excluded.most_common():
        print(f"  excluded {n:>4}  {reason}")
    print(f"Wrote {OUT_DIR / 'ranking.json'} and {OUT_DIR / 'report.md'}")
    return 0


def write_csv(ranked: list) -> None:
    """All ranked businesses in one sheet (opens in Excel; UTF-8 with BOM)."""
    import csv
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    columns = ["rank", "score", "name", "description", "category", "stage", "monthly_revenue_usd",
               "us_dependencies", "channels", "documents", "mentions", "source_title", "source_url",
               "all_sources", "score_parts", "penalties", "revenue_quotes"]
    with open(OUT_DIR / "ranking.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for r in ranked:
            money = [f"{f['kind']} {f['value']:,.0f} {f['currency'] or ''} {f['period']}: \"{' '.join((f['quote'] or '').split())}\""
                     for f in r["figures"] if f["kind"] in ("revenue", "profit")]
            writer.writerow([
                r["rank"], r["score"], r["name"], r["description"], r["category"], r["stage"],
                round(r["monthly_revenue_usd"]) if r["monthly_revenue_usd"] else "",
                "; ".join(r["us_dependencies"]), ", ".join(r["channels"]), r["documents"], r["mentions"],
                r["doc_title"], r["url"], " || ".join(r["all_sources"]),
                ", ".join(f"{k} {v}" for k, v in r["parts"].items()),
                ", ".join(f"{k} -{v}" for k, v in r["penalties"].items()), " || ".join(money)])


def write_report(ranked: list, excluded: Counter, failures: list, cfg: dict, total: int) -> None:
    top = ranked[: cfg["report"]["top_n"]]
    lines = [f"# Ranking: top {len(top)} of {len(ranked)} ranked businesses ({total} after dedupe)", "",
             "Score 0-100 from `config/scoring.yaml`. Unknown data gets neutral credit, never zero. Revenue figures are "
             "claims backed by a quote found in the text, NOT verified by an independent source.", "",
             "| # | Score | Business | Stage | Category | Revenue USD/mo | US deps | Channels | Docs |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in top:
        rev = f"{r['monthly_revenue_usd']:,.0f}" if r["monthly_revenue_usd"] else "-"
        lines.append(f"| {r['rank']} | {r['score']} | {str(r['name'])[:60]} | {r['stage']} | {r['category']} | {rev} | "
                     f"{len(r['us_dependencies'])} | {len(r['channels'])} | {r['documents']} |")
    lines += ["", "## Details", ""]
    for r in top:
        lines += [f"### {r['rank']}. {r['name']} ({r['score']})", r["description"], "",
                  "- Parts: " + ", ".join(f"{k} {v}" for k, v in r["parts"].items()) +
                  (" | Penalties: " + ", ".join(f"{k} -{v}" for k, v in r["penalties"].items()) if r["penalties"] else ""),
                  f"- US dependencies: {'; '.join(r['us_dependencies']) or 'none listed'}",
                  f"- Channels: {', '.join(r['channels'])}",
                  f"- Source: {r['doc_title']}" + (f" ({r['url']})" if r["url"] else "")]
        for f in r["figures"]:
            quote = " ".join((f["quote"] or "").split())
            lines.append(f"- Figure: {f['kind']} {f['value']:,.0f} {f['currency'] or ''} {f['period']} "
                         f"- quote: \"{quote}\"")
        lines.append("")
    lines += ["## Excluded", ""] + [f"- {n} x {reason}" for reason, n in excluded.most_common()]
    lines += ["", f"## Shut down ({len(failures)}), kept as lessons", ""] + \
             [f"- {f['name']}" for f in failures[:50]]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
