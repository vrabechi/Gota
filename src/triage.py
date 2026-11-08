"""Layer 1 of the funnel: cheap LLM triage (title + description + start of transcript)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import filters
import llm

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS_DIR = ROOT / "data" / "transcripts"
TRIAGE_DIR = ROOT / "data" / "triage"

SYSTEM_PROMPT = """\
You triage YouTube videos for a research pipeline. The pipeline looks for concrete online business \
models (software products, info products, content/SEO sites, services and agencies, marketplaces, \
acquisitions of small businesses) that a solo technical operator could replicate.

You only see: title, channel, duration, view count, the start of the description, and the first \
characters of the transcript. Judge from that text alone. Never assume facts that are not in the text.

Return JSON with exactly these fields:
- has_concrete_business: true only if the video centres on at least one specific, identifiable \
business or product (named, or described concretely: what it sells and to whom) that someone built, \
bought or operates. Generic advice, mindset, news, tool tutorials or sales pitches with no such \
business: false.
- has_numbers: true only if the shown text contains concrete figures about a business (revenue, price, \
users, costs, time to results). Vague claims without figures: false.
- speaker_role: "operator" if the main speaker talks about a business they run (a guest founder counts); \
"commentator" if the main speaker analyses other people's businesses, ideas or trends; "interviewer" if \
the format is a host mainly questioning a guest who runs the business; null if the text does not show it.
- relevance_score: integer 0-10 for how useful the video is for finding replicable business models. \
0-2: no business content. 3-5: business content but vague, generic or hard to replicate. 6-8: a concrete \
business with a clear model and some figures. 9-10: several concrete, replicable businesses or one \
described in detail with verifiable-sounding figures.
- reason: one short sentence justifying the score.
"""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_concrete_business": {"type": "boolean"},
        "has_numbers": {"type": "boolean"},
        "speaker_role": {"anyOf": [
            {"type": "string", "enum": ["operator", "commentator", "interviewer"]},
            {"type": "null"},
        ]},
        "relevance_score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["has_concrete_business", "has_numbers", "speaker_role", "relevance_score", "reason"],
    "additionalProperties": False,
}


def load_cfg() -> dict:
    return filters.load_yaml()["layer1"]


# ---------------------------------------------------------------------------- inputs


def load_transcripts(channels: list[str] | None = None) -> list[dict]:
    records = []
    for path in sorted(TRANSCRIPTS_DIR.glob("*/*.json")):
        if channels and path.parent.name not in channels:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        record["_channel_dir"] = path.parent.name
        records.append(record)
    return records


def build_user_text(record: dict, cfg: dict) -> str:
    description = (record.get("description") or "").strip()[: cfg["description_chars"]] or "(not available)"
    transcript = (record.get("transcript_text") or "")[: cfg["transcript_chars"]]
    minutes = round((record.get("duration") or 0) / 60)
    return (f"Title: {record.get('title')}\n"
            f"Channel: {record.get('channel')}\n"
            f"Duration: {minutes} min | Views: {record.get('view_count')}\n\n"
            f"Description (start):\n{description}\n\n"
            f"Transcript (first {cfg['transcript_chars']} characters):\n{transcript}")


def build_params(record: dict, cfg: dict) -> dict:
    params = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_user_text(record, cfg)}],
        "output_config": {"effort": cfg["effort"], "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
    }
    if cfg.get("thinking") == "disabled":
        params["thinking"] = {"type": "disabled"}
    return params


# ----------------------------------------------------------------------------- cache


def cache_path(video_id: str) -> Path:
    return TRIAGE_DIR / f"{video_id}.json"


def load_cached(video_id: str, cfg: dict) -> dict | None:
    """Cached result if it was produced with the current model and prompt_version."""
    entry = llm.read_json(cache_path(video_id))
    if entry and entry.get("model") == cfg["model"] and entry.get("prompt_version") == cfg["prompt_version"]:
        return entry
    return None


def validate_result(raw_text: str) -> dict | None:
    try:
        data = json.loads(raw_text)
        assert isinstance(data["has_concrete_business"], bool)
        assert isinstance(data["has_numbers"], bool)
        assert data["speaker_role"] in ("operator", "commentator", "interviewer", None)
        assert isinstance(data["relevance_score"], int) and 0 <= data["relevance_score"] <= 10
        assert isinstance(data["reason"], str)
        return {k: data[k] for k in TRIAGE_SCHEMA["required"]}
    except (json.JSONDecodeError, KeyError, AssertionError, TypeError):
        return None


def save_result(record: dict, cfg: dict, result: dict, usage, via_batch: bool) -> None:
    llm.write_json(cache_path(record["video_id"]), {
        "video_id": record["video_id"],
        "channel": record.get("channel"),
        "title": record.get("title"),
        "model": cfg["model"],
        "prompt_version": cfg["prompt_version"],
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
        counts = [llm.count_input_tokens(client, cfg["model"], SYSTEM_PROMPT,
                                         requests[i]["messages"][0]["content"]) for i in sample_ids]
        method = f"count_tokens on {len(sample_ids)} of {len(ids)} requests, extrapolated"
    except Exception as exc:  # noqa: BLE001 - missing credentials, network...
        counts = [llm.rough_token_estimate(SYSTEM_PROMPT, requests[i]["messages"][0]["content"])
                  for i in sample_ids]
        method = f"ROUGH heuristic (chars/3.5), count_tokens unavailable: {type(exc).__name__}"
    mean = sum(counts) / len(counts)
    return {"n": len(ids), "input_tokens": mean * len(ids),
            "output_tokens": cfg["est_output_tokens"] * len(ids), "method": method}


# ----------------------------------------------------------------------------- run


def run(channels: list[str] | None, limit: int | None, sync: bool, yes: bool) -> int:
    cfg = load_cfg()
    records = load_transcripts(channels)
    pending = [r for r in records if load_cached(r["video_id"], cfg) is None]
    if limit:
        pending = pending[:limit]
    print(f"Layer 1: {len(records)} transcripts, {len(records) - len(pending)} cached, {len(pending)} to triage.")
    if not pending:
        return 0

    requests = {r["video_id"]: build_params(r, cfg) for r in pending}
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
    by_id = {r["video_id"]: r for r in pending}
    saved = invalid = failed = 0
    if sync:
        for video_id, params in requests.items():
            try:
                message = client.messages.create(**params)
            except Exception as exc:  # noqa: BLE001
                print(f"  {video_id}: API error {type(exc).__name__}: {exc}", file=sys.stderr)
                failed += 1
                continue
            result = validate_result(llm.message_text(message))
            if result is None:
                invalid += 1
                continue
            save_result(by_id[video_id], cfg, result, message.usage, via_batch=False)
            saved += 1
    else:
        batch_id = llm.submit_or_resume_batch(client, "triage", requests)
        llm.wait_for_batch(client, batch_id)
        for video_id, message, error in llm.iter_batch_results(client, batch_id):
            if video_id not in by_id:
                continue
            if message is None:
                print(f"  {video_id}: batch result {error}", file=sys.stderr)
                failed += 1
                continue
            result = validate_result(llm.message_text(message))
            if result is None:
                invalid += 1
                continue
            save_result(by_id[video_id], cfg, result, message.usage, via_batch=True)
            saved += 1
    print(f"Layer 1 done: saved={saved} invalid_output={invalid} failed={failed} (rerun to retry the latter two)")
    return 0
