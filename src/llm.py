"""Shared LLM plumbing: pricing guard, cost estimates, batch submission/resume, JSON cache."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PRICING_FILE = ROOT / "config" / "pricing.yaml"
BATCH_DIR = ROOT / "data" / "batches"

PLACEHOLDER_MARKER = "VERIFICAR"


class PricingNotVerified(RuntimeError):
    """Raised when a paid call would run with unverified (placeholder) prices."""


# ----------------------------------------------------------------------------- pricing


def _is_placeholder(value: object) -> bool:
    return value is None or (isinstance(value, str) and PLACEHOLDER_MARKER in value.upper()) \
        or not isinstance(value, (int, float))


def load_pricing(path: Path = PRICING_FILE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def missing_price_fields(model: str, batch: bool, fields: tuple[str, ...] = ("input", "output"),
                         pricing: dict | None = None) -> list[str]:
    pricing = pricing or load_pricing()
    model_prices = (pricing.get("models") or {}).get(model) or {}
    missing = [f"models.{model}.{f}" for f in fields if _is_placeholder(model_prices.get(f))]
    if batch and _is_placeholder(pricing.get("batch_discount")):
        missing.append("batch_discount")
    return missing


def require_verified_pricing(model: str, batch: bool, fields: tuple[str, ...] = ("input", "output")) -> dict:
    """Refuse to continue if any price needed for this paid call is still a placeholder."""
    pricing = load_pricing()
    missing = missing_price_fields(model, batch, fields, pricing)
    if missing:
        raise PricingNotVerified(
            "Refusing to run a paid call: config/pricing.yaml still has unverified values for "
            + ", ".join(missing) + ". Fill them from the official Anthropic pricing page and re-run.")
    return pricing


def cost_usd(model: str, input_tokens: float, output_tokens: float, batch: bool,
             pricing: dict | None = None) -> float | None:
    """Cost in USD, or None when prices are placeholders (never guess a price)."""
    pricing = pricing or load_pricing()
    if missing_price_fields(model, batch, ("input", "output"), pricing):
        return None
    prices = pricing["models"][model]
    total = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
    return total * pricing["batch_discount"] if batch else total


# ------------------------------------------------------------------------------ client


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines). Real environment variables take precedence."""
    import os
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip("'\""))


def get_client():
    import anthropic
    load_dotenv()
    return anthropic.Anthropic()


def count_input_tokens(client, model: str, system: str, user_text: str) -> int:
    response = client.messages.count_tokens(
        model=model, system=system, messages=[{"role": "user", "content": user_text}])
    return response.input_tokens


def rough_token_estimate(system: str, user_text: str) -> int:
    """Heuristic (~3.5 chars/token). NOT exact: only used when count_tokens is unavailable."""
    return int((len(system) + len(user_text)) / 3.5)


def message_text(message) -> str:
    return next((block.text for block in message.content if block.type == "text"), "")


# ------------------------------------------------------------------------ JSON cache


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)  # atomic


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------ batch


def _batch_state_path(name: str, custom_ids: list[str]) -> Path:
    digest = hashlib.sha256("\n".join(sorted(custom_ids)).encode()).hexdigest()[:12]
    return BATCH_DIR / f"{name}_{digest}.json"


def submit_or_resume_batch(client, name: str, requests: dict[str, dict]) -> str:
    """Submit one batch for these requests, or resume the identical one submitted earlier."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    state_path = _batch_state_path(name, list(requests))
    state = read_json(state_path)
    if state and state.get("batch_id"):
        print(f"Resuming existing batch {state['batch_id']} (submitted {state['submitted_at']}).")
        return state["batch_id"]
    batch = client.messages.batches.create(requests=[
        Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**params))
        for cid, params in requests.items()])
    write_json(state_path, {"batch_id": batch.id, "submitted_at": now_iso(), "n": len(requests)})
    print(f"Submitted batch {batch.id} with {len(requests)} requests.")
    return batch.id


def wait_for_batch(client, batch_id: str, poll_seconds: int = 30) -> None:
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(f"  batch {batch_id}: {batch.processing_status} "
              f"(processing={counts.processing} succeeded={counts.succeeded} errored={counts.errored})",
              file=sys.stderr)
        if batch.processing_status == "ended":
            return
        time.sleep(poll_seconds)


def iter_batch_results(client, batch_id: str):
    """Yield (custom_id, message_or_None, error_text_or_None). Key by custom_id: order is arbitrary."""
    for item in client.messages.batches.results(batch_id):
        if item.result.type == "succeeded":
            yield item.custom_id, item.result.message, None
        else:
            yield item.custom_id, None, item.result.type
