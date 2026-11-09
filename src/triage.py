"""Layer 1 of the funnel: cheap LLM triage (title + metadata + start of the text). Any source."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import documents
import filters
import llm

ROOT = Path(__file__).resolve().parent.parent
TRIAGE_DIR = ROOT / "data" / "triage"

_PROMPT_TEMPLATE = """\
You triage {items} for a research pipeline. The pipeline looks for concrete online business \
models (software products, info products, content/SEO sites, services and agencies, marketplaces, \
acquisitions of small businesses) that a solo technical operator could replicate.

You only see: {seen}. Judge from that text alone. Never assume facts that are not in the text.

Return JSON with exactly these fields:
- has_concrete_business: true only if the {item} centres on at least one specific, identifiable \
business or product (named, or described concretely: what it sells and to whom) that someone built, \
bought or operates. Generic advice, mindset, news, tool tutorials or sales pitches with no such \
business: false.
- has_numbers: true only if the shown text contains concrete figures about a business (revenue, price, \
users, costs, time to results). Vague claims without figures: false.
- relevance_score: integer 0-10 for how useful the {item} is for finding replicable business models. \
0-2: no business content. 3-5: business content but vague, generic or hard to replicate. 6-8: a concrete \
business with a clear model and some figures. 9-10: several concrete, replicable businesses or one \
described in detail with verifiable-sounding figures.
- reason: one short sentence justifying the score.
"""

SOURCE_PROMPT_FIELDS = {
    "youtube": {
        "items": "YouTube videos", "item": "video",
        "seen": "title, channel, duration, view count, the start of the description, and the first "
                "characters of the transcript",
    },
    "hackernews": {
        "items": "Hacker News posts", "item": "post",
        "seen": "title, points, comment count, the post text (if any) and the start of the top-level "
                "comments. Link posts have no text beyond the title: linked pages are not available, "
                "so judge from the title and the comments. A comment's author may be the founder",
    },
}

SYSTEM_PROMPTS = {source: _PROMPT_TEMPLATE.format(**fields) for source, fields in SOURCE_PROMPT_FIELDS.items()}
SYSTEM_PROMPT = SYSTEM_PROMPTS["youtube"]  # kept for backwards compatibility

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_concrete_business": {"type": "boolean"},
        "has_numbers": {"type": "boolean"},
        "relevance_score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["has_concrete_business", "has_numbers", "relevance_score", "reason"],
    "additionalProperties": False,
}


def load_cfg() -> dict:
    return filters.load_yaml()["layer1"]


# ---------------------------------------------------------------------------- inputs


def prompt_version(cfg: dict, source: str) -> int:
    return cfg["prompt_versions"][source]


def build_user_text(doc: dict, cfg: dict) -> str:
    meta, engagement = doc.get("meta") or {}, doc.get("engagement") or {}
    body = (doc.get("text") or "")[: cfg["transcript_chars"]]
    if doc["source"] == "youtube":
        description = (meta.get("description") or "").strip()[: cfg["description_chars"]] or "(not available)"
        minutes = round((meta.get("duration") or 0) / 60)
        language = (meta.get("language") or meta.get("caption_language") or "en").lower()
        # English documents keep the exact old text (cache stays valid); others get a language hint.
        language_note = "" if language.startswith("en") else (
            f"Language: {language} (the text below is not in English; judge it as is and write the reason in English)\n")
        return (language_note +
                f"Title: {doc.get('title')}\n"
                f"Channel: {doc.get('author')}\n"
                f"Duration: {minutes} min | Views: {engagement.get('views')}\n\n"
                f"Description (start):\n{description}\n\n"
                f"Transcript (first {cfg['transcript_chars']} characters):\n{body}")
    return (f"Title: {doc.get('title')}\n"
            f"Source: {doc['source']} | Author: {doc.get('author')} | Points: {engagement.get('score')} | "
            f"Comments: {engagement.get('comments')}\n\n"
            f"Text (first {cfg['transcript_chars']} characters):\n{body or '(no text)'}")


def build_params(doc: dict, cfg: dict) -> dict:
    params = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "system": SYSTEM_PROMPTS[doc["source"]],
        "messages": [{"role": "user", "content": build_user_text(doc, cfg)}],
        "output_config": {"effort": cfg["effort"], "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
    }
    if cfg.get("thinking") == "disabled":
        params["thinking"] = {"type": "disabled"}
    return params


# ----------------------------------------------------------------------------- cache


def cache_path(key: str) -> Path:
    return TRIAGE_DIR / f"{key}.json"


def load_cached(doc: dict, cfg: dict) -> dict | None:
    """Cached result if it was produced with the current model and the source's prompt_version."""
    entry = llm.read_json(cache_path(documents.doc_key(doc)))
    if entry and entry.get("model") == cfg["model"] \
            and entry.get("prompt_version") == prompt_version(cfg, doc["source"]):
        return entry
    return None


def validate_result(raw_text: str) -> dict | None:
    try:
        data = json.loads(raw_text)
        assert isinstance(data["has_concrete_business"], bool)
        assert isinstance(data["has_numbers"], bool)
        assert isinstance(data["relevance_score"], int) and 0 <= data["relevance_score"] <= 10
        assert isinstance(data["reason"], str)
        return {k: data[k] for k in TRIAGE_SCHEMA["required"]}
    except (json.JSONDecodeError, KeyError, AssertionError, TypeError):
        return None


def save_result(doc: dict, cfg: dict, result: dict, usage, via_batch: bool) -> None:
    llm.write_json(cache_path(documents.doc_key(doc)), {
        "video_id": doc["source_id"],   # historical name, kept so old cache entries stay readable
        "source": doc["source"],
        "channel": doc.get("author"),
        "title": doc.get("title"),
        "model": cfg["model"],
        "prompt_version": prompt_version(cfg, doc["source"]),
        "result": result,
        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
        "via_batch": via_batch,
        "ts": llm.now_iso(),
    })


def passes_layer1(entry: dict, cfg: dict) -> bool:
    result = entry["result"]
    if cfg["require_concrete_business"] and not result["has_concrete_business"]:
        return False
    return result["relevance_score"] >= cfg["min_relevance_score"]


# ------------------------------------------------------------------------- estimate


def estimate_tokens(requests: dict[str, dict], cfg: dict) -> dict:
    """Exact count_tokens on a sample, extrapolated to all requests; heuristic if no credentials."""
    ids = list(requests)
    sample_ids = ids[: cfg["estimate_sample_size"]]
    try:
        client = llm.get_client()
        counts = [llm.count_input_tokens(client, cfg["model"], requests[i]["system"],
                                         requests[i]["messages"][0]["content"]) for i in sample_ids]
        method = f"count_tokens on {len(sample_ids)} of {len(ids)} requests, extrapolated"
    except Exception as exc:  # noqa: BLE001 - missing credentials, network...
        counts = [llm.rough_token_estimate(requests[i]["system"], requests[i]["messages"][0]["content"])
                  for i in sample_ids]
        method = f"ROUGH heuristic (chars/3.5), count_tokens unavailable: {type(exc).__name__}"
    mean = sum(counts) / len(counts)
    return {"n": len(ids), "input_tokens": mean * len(ids),
            "output_tokens": cfg["est_output_tokens"] * len(ids), "method": method}


# ----------------------------------------------------------------------------- run


def run(channels: list[str] | None, limit: int | None, sync: bool, yes: bool,
        sources: list[str] | None = None) -> int:
    cfg = load_cfg()
    docs = documents.load_all(sources, channels)
    pending = [d for d in docs if load_cached(d, cfg) is None]
    if limit:
        pending = pending[:limit]
    print(f"Layer 1: {len(docs)} documents, {len(docs) - len(pending)} cached, {len(pending)} to triage.")
    if not pending:
        return 0

    requests = {documents.doc_key(d): build_params(d, cfg) for d in pending}
    estimate = estimate_tokens(requests, cfg)
    batch = not sync
    cost = llm.cost_usd(cfg["model"], estimate["input_tokens"], estimate["output_tokens"], batch)
    print(f"Estimate ({cfg['model']}, {'batch' if batch else 'sync, no batch discount'}): "
          f"~{estimate['input_tokens']:,.0f} input + ~{estimate['output_tokens']:,.0f} output tokens "
          f"[{estimate['method']}]")
    print("Estimated cost: " + (f"US$ {cost:.4f}" if cost is not None else
          "unavailable (config/pricing.yaml has VERIFICAR placeholders)"))

    if not yes:
        print("Estimate only. Re-run with --yes to execute the paid calls.")
        return 0
    try:
        llm.require_verified_pricing(cfg["model"], batch)
    except llm.PricingNotVerified as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    client = llm.get_client()
    by_key = {documents.doc_key(d): d for d in pending}
    saved = invalid = failed = 0
    if sync:
        for key, params in requests.items():
            try:
                message = client.messages.create(**params)
            except Exception as exc:  # noqa: BLE001
                print(f"  {key}: API error {type(exc).__name__}: {exc}", file=sys.stderr)
                failed += 1
                continue
            result = validate_result(llm.message_text(message))
            if result is None:
                invalid += 1
                continue
            save_result(by_key[key], cfg, result, message.usage, via_batch=False)
            saved += 1
    else:
        batch_id = llm.submit_or_resume_batch(client, "triage", requests)
        llm.wait_for_batch(client, batch_id)
        for key, message, error in llm.iter_batch_results(client, batch_id):
            if key not in by_key:
                continue
            if message is None:
                print(f"  {key}: batch result {error}", file=sys.stderr)
                failed += 1
                continue
            result = validate_result(llm.message_text(message))
            if result is None:
                invalid += 1
                continue
            save_result(by_key[key], cfg, result, message.usage, via_batch=True)
            saved += 1
    print(f"Layer 1 done: saved={saved} invalid_output={invalid} failed={failed} (rerun to retry the latter two)")
    return 0
