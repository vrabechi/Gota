"""Layer 2: structured extraction of the businesses described in a document (Sonnet).

One call per document returns a list of businesses. Numbers carry a supporting_quote that CODE checks against
the source text; an unverifiable number is nulled but the business is always kept.
"""
from __future__ import annotations

import json
import random
import re
import sys
import unicodedata
from pathlib import Path

import yaml
from rapidfuzz import fuzz

import documents
import llm
import triage

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "extraction.yaml"
EXTRACTION_DIR = ROOT / "data" / "extractions"

# ------------------------------------------------------------------------------ vocabularies

CATEGORIES = ["software_saas", "mobile_app", "info_product_course", "content_media_seo", "newsletter",
              "agency_service", "productized_service", "marketplace_platform", "ecommerce_physical",
              "dropshipping_pod", "small_business_acquisition", "local_service_business", "creator_business",
              "community_membership", "other"]
STAGES = ["idea", "pre_revenue", "early_revenue", "growing", "mature", "exited", "shut_down", "unknown"]
PROMINENCE = ["main_subject", "secondary", "mentioned_in_passing"]
CUSTOMER_TYPES = ["b2b", "b2c", "both", "unknown"]
PLATFORM_ROLES = ["payments", "hosting_infrastructure", "marketplace_sales_channel", "ecommerce_platform",
                  "no_code_tool", "ai_model_api", "advertising", "social_distribution", "email_marketing",
                  "analytics", "communication", "crm", "other"]
MONETIZATION = ["subscription", "one_time_sale", "usage_based", "transaction_commission", "advertising",
                "affiliate", "sponsorship", "services_fees", "licensing", "lead_generation", "freemium_upsell",
                "other"]
CHANNELS = ["seo", "paid_ads", "social_organic", "marketplace_listing", "cold_outreach", "partnerships",
            "email_list", "community", "referral_word_of_mouth", "content_youtube", "app_store", "pr_press", "other"]
LEVELS = ["low", "medium", "high", "unknown"]
PHYSICAL_OPERATIONS = ["none", "outsourced", "own", "unknown"]
REVENUE_SOURCES = ["customers", "sponsors_ads", "sells_courses_about_it", "investors", "mixed", "unknown"]
METRIC_KINDS = ["revenue", "profit", "gross_margin", "costs", "ad_spend", "cac", "ltv", "churn",
                "conversion_rate", "initial_capital", "price", "customers", "users", "traffic", "growth_rate",
                "team_size", "founder_hours_per_week", "time_to_first_revenue", "exit_price", "other"]
METRIC_UNITS = ["currency", "percent", "count", "hours", "days", "months", "years", "other"]
METRIC_PERIODS = ["one_time", "day", "week", "month", "year", "cumulative", "unspecified"]


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}


def _enum(values: list[str]) -> dict:
    return {"type": "string", "enum": values}


METRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": _enum(METRIC_KINDS),
        "label": _STR,
        "value": _nullable({"type": "number"}),
        "currency": _nullable(_STR),
        "unit": _enum(METRIC_UNITS),
        "period": _enum(METRIC_PERIODS),
        "supporting_quote": _nullable(_STR),
    },
    "required": ["kind", "label", "value", "currency", "unit", "period", "supporting_quote"],
    "additionalProperties": False,
}

BUSINESS_SCHEMA = {
    "type": "object",
    "properties": {
        "name": _nullable(_STR),
        "description": _STR,
        "category": _enum(CATEGORIES),
        "stage": _enum(STAGES),
        "started": _nullable(_STR),
        "prominence": _enum(PROMINENCE),
        "customer": {
            "type": "object",
            "properties": {"type": _enum(CUSTOMER_TYPES), "segment": _nullable(_STR), "problem": _nullable(_STR)},
            "required": ["type", "segment", "problem"], "additionalProperties": False},
        "platforms": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": _STR, "role": _enum(PLATFORM_ROLES)},
            "required": ["name", "role"], "additionalProperties": False}},
        "single_platform_dependency": _nullable({"type": "boolean"}),
        "monetization_models": {"type": "array", "items": _enum(MONETIZATION)},
        "metrics": {"type": "array", "items": METRIC_SCHEMA},
        "acquisition_channels": {"type": "array", "items": _enum(CHANNELS)},
        "operation": {
            "type": "object",
            "properties": {
                "automation_level": _enum(LEVELS), "support_load": _enum(LEVELS),
                "skills_required": _STR_LIST,
                "physical_operations": _enum(PHYSICAL_OPERATIONS),
                "regulated_industry": _nullable(_STR),
                "revenue_source": _enum(REVENUE_SOURCES),
                "us_specific_dependencies": _STR_LIST,
                "language_dependent": _nullable({"type": "boolean"}),
                "relies_on_existing_audience": _nullable({"type": "boolean"}),
                "entry_barriers": _STR_LIST,
            },
            "required": ["automation_level", "support_load", "skills_required", "physical_operations",
                         "regulated_industry", "revenue_source", "us_specific_dependencies",
                         "language_dependent", "relies_on_existing_audience", "entry_barriers"],
            "additionalProperties": False},
        "competitors_mentioned": _STR_LIST,
        "risks_or_failures": _STR_LIST,
    },
    "required": ["name", "description", "category", "stage", "started", "prominence", "customer", "platforms",
                 "single_platform_dependency", "monetization_models", "metrics", "acquisition_channels",
                 "operation", "competitors_mentioned", "risks_or_failures"],
    "additionalProperties": False,
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {"businesses": {"type": "array", "items": BUSINESS_SCHEMA}},
    "required": ["businesses"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You extract structured facts about online businesses from one document (a video transcript, forum \
thread, blog article...) for a research pipeline that looks for business models someone could replicate.

Rules:
- Extract every business, product or company in the document that has an identifiable model (what it \
sells, to whom, how it earns). Include it even when there are no figures. Skip things mentioned only in \
passing (an analogy, a name-drop, a one-line example) and large companies cited only as context. Return at most {max_businesses}, the most detailed first. \
If there is none, return an empty list.
- Use only what the text says. Never fill gaps from your own knowledge and never guess. Unknown or \
not stated: null (or "unknown" / an empty list where the field requires it).
- The transcript may come from automatic captions: expect missing punctuation and misheard words. \
Resolve obvious mishearings of company names only when the text itself makes it clear.
- Write all fields in English. supporting_quote must be copied verbatim from the text, in its original \
language, as one contiguous passage (at most 300 characters).
- metrics: one entry per figure stated about the business (revenue, profit, costs, price, customers, \
conversion, team size, founder hours per week, time to first revenue, capital needed, exit price...). \
value is a plain number (10k = 10000; a percentage as 35 for 35%). currency is an ISO code when the text \
makes it clear, otherwise null. Every metric with a value needs a supporting_quote that contains the \
figure; if you cannot quote it, set value to null. Do not convert currencies.
- platforms: the tools, marketplaces and services the business actually runs on (payments, hosting, \
sales channels, no-code tools, AI APIs, ad networks...). single_platform_dependency is true only if the \
text says the business depends on one platform it does not control.
- operation: revenue_source is where the money comes from; "sells_courses_about_it" when the revenue \
is mainly from teaching others how to do it. relies_on_existing_audience: true if the text says the \
business got its customers mainly through an audience, community, mailing list, follower base or sister \
products that the founder already had; false if the text says it grew from no audience; null if the text \
does not say. us_specific_dependencies lists things the text ties to the \
US (payment rails, insurance or health systems, tax or legal structure, US-only marketplaces or data). \
entry_barriers lists capital, licences, skills or relationships the text says are needed.
- prominence: "main_subject" if the document is mainly about this business, "secondary" if it gets a \
real section. Do not return businesses that are only mentioned in passing.
- description: one sentence, at most 200 characters: what it sells, to whom, how it earns.
- operation.physical_operations: "none" = software, content, digital products or services delivered \
remotely; "outsourced" = there is a physical product or on-site work but third parties manufacture, \
store, ship or perform it (dropshipping, print-on-demand, contractors); "own" = the operator \
manufactures, stocks, installs, cooks, builds or runs a location or a field crew. Servers, computers, \
cloud, GPUs and other IT infrastructure do NOT count as physical; "unknown" if the text does not say.
- Do not rank, judge or comment. No opinions about any country or market.
"""


# ------------------------------------------------------------------------------ config & inputs


def load_cfg() -> dict:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))


def source_text(doc: dict, cfg: dict) -> tuple[str, bool]:
    """Full text (capped) and whether it was truncated."""
    text = doc.get("text") or ""
    cap = cfg["input"]["max_chars"]
    return text[:cap], len(text) > cap


def build_user_text(doc: dict, cfg: dict) -> str:
    text, truncated = source_text(doc, cfg)
    meta = doc.get("meta") or {}
    head = [f"Source: {doc['source']}", f"Title: {doc.get('title')}", f"Author/channel: {doc.get('author')}",
            f"Date: {doc.get('date')}"]
    language = (meta.get("language") or meta.get("caption_language") or "").lower()
    if language and not language.startswith("en"):
        head.append(f"Language of the text: {language}")
    if doc["source"] == "youtube":
        description = (meta.get("description") or "").strip()[: cfg["input"]["description_chars"]]
        if description:
            head.append(f"Description (start):\n{description}")
    note = "\n[TEXT TRUNCATED: the rest of the document is not shown]" if truncated else ""
    return "\n".join(head) + f"\n\nTEXT:\n{text}{note}"


def build_params(doc: dict, cfg: dict) -> dict:
    params = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "system": SYSTEM_PROMPT.format(max_businesses=cfg["input"]["max_businesses"]),
        "messages": [{"role": "user", "content": build_user_text(doc, cfg)}],
        "output_config": {"effort": cfg["effort"], "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
    }
    if cfg.get("thinking") == "disabled":
        # Sonnet 5.5 rejects {"type": "disabled"}; "between_tools" is its way to not think before answering.
        params["thinking"] = {"type": "disabled" if "haiku" in cfg["model"] else "between_tools"}
    elif cfg.get("thinking") == "adaptive":
        params["thinking"] = {"type": "adaptive"}
    return params


# ------------------------------------------------------------------------ quote verification


def normalise(text: str) -> str:
    """Case, accents-as-is, punctuation and whitespace removed: auto captions and quotes differ in those."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", text)).strip()


def verify_quote(quote: str | None, haystack_norm: str, min_score: float) -> tuple[bool, float]:
    if not quote:
        return False, 0.0
    needle = normalise(quote)
    if not needle:
        return False, 0.0
    if needle in haystack_norm:
        return True, 100.0
    score = fuzz.partial_ratio(needle, haystack_norm)
    return score >= min_score, float(score)


def verify_businesses(businesses: list[dict], full_text: str, cfg: dict) -> tuple[list[dict], dict]:
    """Null the value of every metric whose quote is not in the text. Businesses are never dropped."""
    haystack = normalise(full_text)
    stats = {"metrics": 0, "with_value": 0, "verified": 0, "nulled": 0}
    out = []
    for business in businesses:
        business = json.loads(json.dumps(business))  # deep copy
        for metric in business.get("metrics", []):
            stats["metrics"] += 1
            if metric.get("value") is None:
                metric["quote_verified"] = None
                continue
            stats["with_value"] += 1
            ok, score = verify_quote(metric.get("supporting_quote"), haystack, cfg["verification"]["min_match_score"])
            metric["quote_match_score"] = round(score, 1)
            metric["quote_verified"] = ok
            if ok:
                stats["verified"] += 1
            else:
                stats["nulled"] += 1
                metric["unverified_value"] = metric["value"]
                metric["value"] = None
        out.append(business)
    return out, stats


# ------------------------------------------------------------------------------ cache


def cache_path(doc: dict, model: str) -> Path:
    return EXTRACTION_DIR / model / f"{documents.doc_key(doc)}.json"


def load_cached(doc: dict, cfg: dict) -> dict | None:
    entry = llm.read_json(cache_path(doc, cfg["model"]))
    if entry and entry.get("model") == cfg["model"] and entry.get("prompt_version") == cfg["prompt_version"]:
        return entry
    return None


def parse_output(message) -> list[dict] | None:
    if getattr(message, "stop_reason", None) == "max_tokens":
        return None
    try:
        data = json.loads(llm.message_text(message))
        businesses = data["businesses"]
        assert isinstance(businesses, list)
        return businesses
    except (json.JSONDecodeError, KeyError, AssertionError, TypeError):
        return None


def save(doc: dict, cfg: dict, raw: list[dict], usage, via_batch: bool) -> dict:
    text, truncated = source_text(doc, cfg)
    businesses, stats = verify_businesses(raw, text, cfg)
    entry = {
        "source": doc["source"], "source_id": doc["source_id"], "title": doc.get("title"),
        "author": doc.get("author"), "date": doc.get("date"), "url": doc.get("url"),
        "model": cfg["model"], "prompt_version": cfg["prompt_version"],
        "truncated": truncated, "businesses": businesses, "verification": stats,
        "raw_businesses": raw,
        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
        "via_batch": via_batch, "ts": llm.now_iso(),
    }
    llm.write_json(cache_path(doc, cfg["model"]), entry)
    return entry


# ------------------------------------------------------------------------------ selection


def select_documents(pool: str, sources: list[str] | None, channels: list[str] | None,
                     cfg: dict) -> list[dict]:
    """Documents that passed Layer 1 (pool=passed) or that were triaged just under the cutoff (pool=below)."""
    l1 = triage.load_cfg()
    chosen = []
    for doc in documents.load_all(sources, channels):
        entry = triage.load_cached(doc, l1)
        if not entry:
            continue
        passed = triage.passes_layer1(entry, l1)
        score = entry["result"]["relevance_score"]
        if pool == "passed" and passed:
            chosen.append(doc)
        elif pool == "below" and not passed and score >= cfg["selection"]["recall_below_min_score"]:
            chosen.append(doc)
    return chosen


# ------------------------------------------------------------------------------ run


def run(pool: str, sources: list[str] | None, channels: list[str] | None, limit: int | None,
        sample: int | None, sync: bool, yes: bool) -> int:
    cfg = load_cfg()
    candidates = select_documents(pool, sources, channels, cfg)
    pending = [d for d in candidates if load_cached(d, cfg) is None]
    if sample:
        random.Random(cfg["selection"]["sample_seed"]).shuffle(pending)
        pending = pending[:sample]
    elif limit:
        pending = pending[:limit]
    print(f"Layer 2 ({pool}): {len(candidates)} eligible, {len(candidates) - len([d for d in candidates if load_cached(d, cfg) is None])} "
          f"already extracted, {len(pending)} to extract now.")
    if not pending:
        return 0

    requests = {documents.doc_key(d): build_params(d, cfg) for d in pending}
    ids = list(requests)[: cfg["estimate_sample_size"]]
    try:
        client = llm.get_client()
        counts = [llm.count_input_tokens(client, cfg["model"], requests[i]["system"],
                                         requests[i]["messages"][0]["content"]) for i in ids]
        method = f"count_tokens on {len(ids)} of {len(requests)}, extrapolated"
    except Exception as exc:  # noqa: BLE001
        counts = [llm.rough_token_estimate(requests[i]["system"], requests[i]["messages"][0]["content"]) for i in ids]
        method = f"ROUGH heuristic (chars/3.5), count_tokens unavailable: {type(exc).__name__}"
    batch = not sync
    tokens_in = sum(counts) / len(counts) * len(requests)
    tokens_out = cfg["est_output_tokens"] * len(requests)
    cost = llm.cost_usd(cfg["model"], tokens_in, tokens_out, batch)
    print(f"Estimate ({cfg['model']}, {'batch' if batch else 'sync, no batch discount'}): "
          f"~{tokens_in:,.0f} input + ~{tokens_out:,.0f} output tokens [{method}]")
    print("Estimated cost: " + (f"US$ {cost:.2f}" if cost is not None else "unavailable (pricing placeholders)"))
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
    totals = {"metrics": 0, "with_value": 0, "verified": 0, "nulled": 0}

    def handle(key: str, message, via_batch: bool) -> None:
        nonlocal saved, invalid
        raw = parse_output(message)
        if raw is None:
            invalid += 1
            return
        entry = save(by_key[key], cfg, raw, message.usage, via_batch)
        saved += 1
        for name, value in entry["verification"].items():
            totals[name] += value

    if sync:
        for key, params in requests.items():
            try:
                handle(key, client.messages.create(**params), False)
            except Exception as exc:  # noqa: BLE001
                print(f"  {key}: API error {type(exc).__name__}: {exc}", file=sys.stderr)
                failed += 1
    else:
        batch_id = llm.submit_or_resume_batch(client, "extract", requests)
        llm.wait_for_batch(client, batch_id)
        for key, message, error in llm.iter_batch_results(client, batch_id):
            if key not in by_key:
                continue
            if message is None:
                print(f"  {key}: batch result {error}", file=sys.stderr)
                failed += 1
                continue
            handle(key, message, True)
    print(f"Layer 2 done: saved={saved} invalid_output={invalid} failed={failed} (rerun to retry the latter two)")
    print(f"Numbers: {totals['with_value']} with value, {totals['verified']} quote-verified, "
          f"{totals['nulled']} nulled (quote not found); {totals['metrics']} metrics in total")
    return 0
