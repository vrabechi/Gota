"""Dedupe the businesses extracted by Layer 2: the same company shows up in several documents.

Free step (local embeddings + rapidfuzz). Output in data/dedupe/:
  clusters.json  one entry per distinct business: canonical record, members, documents, channels
  review.csv     ambiguous pairs for manual review (never merged automatically)
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import yaml

import documents
import llm

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "dedupe.yaml"
EXTRACTION_DIR = ROOT / "data" / "extractions"
OUT_DIR = ROOT / "data" / "dedupe"

PROMINENCE_RANK = {"main_subject": 2, "secondary": 1, "mentioned_in_passing": 0}


def load_cfg() -> dict:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))


def normalise_name(name: str | None, stopwords: set[str]) -> str:
    """casefold, drop parentheticals and punctuation, drop legal/filler words. '' when no usable name."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(w for w in text.split() if w not in stopwords)


def load_records(cfg: dict) -> list[dict]:
    """One record per extracted business, with the document it came from."""
    stop = set(cfg["name_stopwords"])
    by_key = {documents.doc_key(d): d for d in documents.load_all()}
    records = []
    for path in sorted((EXTRACTION_DIR / cfg["extraction_model"]).glob("*.json")):
        entry = llm.read_json(path)
        doc = by_key.get(path.stem)
        if not entry or not doc:
            continue
        for i, business in enumerate(entry["businesses"]):
            meta = doc.get("meta") or {}
            records.append({
                "rid": f"{path.stem}#{i}", "doc_key": path.stem, "source": doc["source"],
                "channel": meta.get("channel_slug") or doc["source"], "doc_title": doc.get("title"),
                "url": doc.get("url"), "date": doc.get("date"),
                "name": business.get("name"), "name_key": normalise_name(business.get("name"), stop),
                "description": business.get("description") or "",
                "prominence": business.get("prominence"), "stage": business.get("stage"),
                "n_metrics": sum(1 for m in business.get("metrics", []) if m.get("value") is not None),
            })
    return records


def embed(records: list[dict], model_name: str):
    import numpy as np
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name)
    texts = [f"{r['name'] or ''}. {r['description']}".strip(". ") for r in records]
    return np.array(list(model.embed(texts)), dtype="float32")  # bge vectors are already L2-normalised


def name_score(a: dict, b: dict) -> float | None:
    from rapidfuzz import fuzz
    if not a["name_key"] or not b["name_key"]:
        return None
    return max(fuzz.ratio(a["name_key"], b["name_key"]), fuzz.token_sort_ratio(a["name_key"], b["name_key"]))


def is_subset(a: dict, b: dict) -> bool:
    ta, tb = set(a["name_key"].split()), set(b["name_key"].split())
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def rule_matches(rule: dict, score: float | None, cosine: float, exact: bool, subset: bool) -> bool:
    if cosine < rule.get("min_cosine", 0):
        return False
    if rule.get("exact_name") and not exact:
        return False
    if rule.get("name_subset") and not subset:
        return False
    if "min_name_score" in rule and (score is None or score < rule["min_name_score"]):
        return False
    return True


def find_pairs(records: list[dict], vectors, cfg: dict) -> tuple[list, list]:
    import numpy as np
    sims = vectors @ vectors.T
    rows, cols = np.where(np.triu(sims >= cfg["candidate_cosine"], k=1))
    auto, review = [], []
    for i, j in zip(rows.tolist(), cols.tolist()):
        a, b = records[i], records[j]
        if a["doc_key"] == b["doc_key"]:
            continue
        cosine = float(sims[i, j])
        score = name_score(a, b)
        exact = bool(a["name_key"]) and a["name_key"] == b["name_key"]
        subset = is_subset(a, b)
        if any(rule_matches(r, score, cosine, exact, subset) for r in cfg["auto_merge"]):
            auto.append((i, j, cosine, score))
        elif any(rule_matches(r, score, cosine, exact, subset) for r in cfg["review"]):
            review.append((i, j, cosine, score))
    return auto, review


def cluster(n: int, pairs: list) -> list[int]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, *_ in pairs:
        parent[find(i)] = find(j)
    return [find(i) for i in range(n)]


def canonical(members: list[dict]) -> dict:
    """The record that best represents a cluster: most prominent, then most numbers, then longest text."""
    return max(members, key=lambda r: (PROMINENCE_RANK.get(r["prominence"], 0), r["n_metrics"],
                                       bool(r["name"]), len(r["description"])))


ADJ_FILE = OUT_DIR / "adjudication.json"
ADJ_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["same", "different", "unsure"]},
                   "reason": {"type": "string"}},
    "required": ["verdict", "reason"], "additionalProperties": False,
}
ADJ_SYSTEM = """You decide whether two records extracted from different documents describe the SAME real-world business, product or company. Answer "same" only if they are clearly one entity (name variants, different spellings, a domain vs its brand, a person's company vs its own name). Answer "different" if they are separate companies or separate instances of a generic model (two operators of similar agencies, two stores of the same kind), or one is a parent/part of the other. "unsure" when the text does not settle it. Judge from the text only. reason: one short sentence, in English."""


def _pair_key(a: dict, b: dict) -> str:
    return "|".join(sorted([a["rid"], b["rid"]]))


def adjudicate(yes: bool) -> int:
    """Ask Haiku about each ambiguous pair. Cached per pair; 'same' verdicts are applied by the next `dedupe` run."""
    cfg = load_cfg()
    records = load_records(cfg)
    auto, review = find_pairs(records, embed(records, cfg["embedding_model"]), cfg)
    roots = cluster(len(records), auto)
    cache = (llm.read_json(ADJ_FILE) or {}).get("pairs", {})
    todo = [(records[i], records[j]) for i, j, *_ in review
            if roots[i] != roots[j] and _pair_key(records[i], records[j]) not in cache]
    model = cfg["adjudication"]["model"]
    est_in, est_out = len(todo) * 450, len(todo) * 60
    cost = llm.cost_usd(model, est_in, est_out, batch=False)
    print(f"{len(todo)} pairs to judge ({len(cache)} cached). Estimate ({model}, sync): ~{est_in:,} input + "
          f"~{est_out:,} output tokens, cost "
          + (f"US$ {cost:.3f}" if cost is not None else "unavailable (pricing not verified)") + " [rough token estimate]")
    if not todo:
        return 0
    if not yes:
        print("Estimate only. Re-run with --yes to execute the paid calls.")
        return 0
    llm.require_verified_pricing(model, batch=False)
    client = llm.get_client()
    for a, b in todo:
        def side(r: dict) -> str:
            return "\n".join([f"name: {r['name']}", f"description: {r['description']}", f"document: {r['doc_title']}"])
        params = {
            "model": model, "max_tokens": 200, "system": ADJ_SYSTEM,
            "messages": [{"role": "user", "content": "Record A\n" + side(a) + "\n\nRecord B\n" + side(b)}],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": ADJ_SCHEMA}},
            "thinking": {"type": "disabled"},
        }
        try:
            result = json.loads(llm.message_text(client.messages.create(**params)))
        except Exception as exc:  # noqa: BLE001 - keep going; the pair stays in review
            print(f"  {_pair_key(a, b)}: {type(exc).__name__}")
            continue
        cache[_pair_key(a, b)] = {**result, "name_a": a["name"], "name_b": b["name"]}
        llm.write_json(ADJ_FILE, {"model": model, "pairs": cache})
    counts = {v: sum(1 for r in cache.values() if r["verdict"] == v) for v in ("same", "different", "unsure")}
    print(f"Judged. Totals: {counts}. Run `dedupe` to apply the 'same' verdicts.")
    return 0


def run(report_only: bool = False) -> int:
    cfg = load_cfg()
    records = load_records(cfg)
    if not records:
        print(f"No extractions found in data/extractions/{cfg['extraction_model']}/")
        return 1
    vectors = embed(records, cfg["embedding_model"])
    auto, review = find_pairs(records, vectors, cfg)
    verdicts = (llm.read_json(ADJ_FILE) or {}).get("pairs", {})
    by_rid = {r["rid"]: i for i, r in enumerate(records)}
    merge_verdicts = {"same", "unsure"} if cfg["adjudication"].get("merge_unsure") else {"same"}
    judged_same = [(by_rid[k.split("|")[0]], by_rid[k.split("|")[1]]) for k, v in verdicts.items()
                   if v["verdict"] in merge_verdicts and all(x in by_rid for x in k.split("|"))]
    review = [p for p in review
              if verdicts.get(_pair_key(records[p[0]], records[p[1]]), {}).get("verdict", "unsure") == "unsure"]
    roots = cluster(len(records), auto + judged_same)
    groups: dict[int, list[dict]] = defaultdict(list)
    for record, root in zip(records, roots):
        groups[root].append(record)

    clusters = []
    for members in groups.values():
        head = canonical(members)
        clusters.append({
            "cluster_id": head["rid"], "name": head["name"], "description": head["description"],
            "mentions": len(members), "documents": len({m["doc_key"] for m in members}),
            "channels": sorted({m["channel"] for m in members}),
            "members": [m["rid"] for m in members],
        })
    clusters.sort(key=lambda c: (-c["documents"], c["cluster_id"]))

    root_of = {r["rid"]: root for r, root in zip(records, roots)}
    review_rows = [(records[i], records[j], cosine, score) for i, j, cosine, score in review
                   if root_of[records[i]["rid"]] != root_of[records[j]["rid"]]]
    review_rows.sort(key=lambda row: -row[2])

    print(f"{len(records)} records -> {len(clusters)} distinct businesses "
          f"({len(auto)} auto + {len(judged_same)} model-judged merges); {len(review_rows)} pairs left for manual review")
    multi = [c for c in clusters if c["documents"] > 1]
    print(f"{len(multi)} businesses appear in 2+ documents; top: " +
          ", ".join(f"{c['name']} ({c['documents']})" for c in multi[:8]))
    if report_only:
        return 0
    llm.write_json(OUT_DIR / "clusters.json", {"extraction_model": cfg["extraction_model"], "clusters": clusters,
                                              "records": {r["rid"]: r for r in records}})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "review.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["cosine", "name_score", "name_a", "name_b", "description_a", "description_b",
                         "doc_a", "doc_b", "rid_a", "rid_b", "decision"])
        for a, b, cosine, score in review_rows:
            writer.writerow([round(cosine, 3), None if score is None else round(score), a["name"], b["name"],
                             a["description"], b["description"], a["doc_title"], b["doc_title"],
                             a["rid"], b["rid"], ""])
    print(f"Wrote {OUT_DIR / 'clusters.json'} and {OUT_DIR / 'review.csv'}")
    return 0
