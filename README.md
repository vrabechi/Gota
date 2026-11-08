# Gota

Pipeline to find online business models that work in the US but are still underdeveloped in Brazil.

Funnel: **Layer 0** (free metadata filters) -> download transcripts -> **Layer 1** (Haiku triage) -> **Layer 2** (full extraction, pending) -> ranking (pending).

## Setup

```
uv sync
```

Paid steps need `ANTHROPIC_API_KEY` (or `ant auth login`) and verified prices in `config/pricing.yaml`.

## Config files

| File | What it controls |
|---|---|
| `config/channels.yaml` | channels, `max_videos`, `min_date`, sleep/retry settings, `scan_limit` |
| `config/filters.yaml` | Layer 0 rules (duration, views percentile, title regex boost/penalize) and Layer 1 (model, thresholds, prompt version) |
| `config/pricing.yaml` | USD per 1M tokens. Values marked `VERIFICAR` block every paid call until you fill them |

## Commands

```
# Layer 0: report on the fast channel listing (no downloads, no cost)
uv run python src/cli.py layer0 [--channel starter-story] [--show-kept 10]

# Download transcripts of the videos that survive Layer 0
uv run python src/download.py [--channel NAME] [--max-videos N] [--dry-run] [--retry-failed] [--cookies-from-browser chrome]

# Layer 1 triage. Without --yes it only prints the estimate.
uv run python src/cli.py triage [--channel NAME] [--limit N] [--sync] [--yes]

# Entries/exits and cost per layer; --details lists each triaged video
uv run python src/cli.py funnel [--details]
```

## Behaviour

- **Resumable and cached.** Downloads skip existing files and permanent failures (`data/failures.jsonl`). Layer 1 results are cached in `data/triage/<video_id>.json`; changing thresholds never re-calls the API, only bumping `layer1.prompt_version` or the model does. Batches are resumed from `data/batches/`.
- **Layer 0** attributes each dropped video to the first stage that removed it (duration, then views percentile per channel, then title score) and reports how often each title rule matched, dropped and kept videos.
- **Batch vs `--sync`.** Batch is cheaper but can take up to an hour; `--sync` is immediate and full price, handy for small samples.
- **Cost safety.** `triage` prints a token and cost estimate first. With `VERIFICAR` placeholders in `pricing.yaml` the USD estimate is unavailable and `--yes` is refused.
- Output: `data/transcripts/<channel>/<video_id>.json`, `data/triage/`, `data/funnel/layer0_<channel>.json`, logs in `data/logs/`.
- `views_per_day` needs the upload date, which the fast yt-dlp listing does not provide; when missing it falls back to raw views (counted in the Layer 0 report).
