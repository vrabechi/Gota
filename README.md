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
| `config/channels.yaml` | channels, `min_date`, sleep/retry settings, `scan_limit`, optional per-channel `max_videos` and `languages` |
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

## Sources

All sources are converted to one document format (`src/documents.py`: source, source_id, url, date, author, title, text, engagement, meta), so triage and the funnel work on any of them.

| Source | Command | Config | Status |
|---|---|---|---|
| YouTube | `src/download.py` | `config/channels.yaml` | working (`yt-dlp`, unofficial) |
| Hacker News | `uv run python src/cli.py collect --source hackernews [--query NAME] [--limit N] [--dry-run]` | `config/sources.yaml` | working; terms of use of the Algolia API not verified |
| Product Hunt | - | - | not built: API forbids commercial use, token needed, scraping forbidden by its ToS |
| Reddit | - | - | not built: needs Reddit approval (Responsible Builder Policy) |

Hacker News notes: the revenue-oriented queries return very few posts (about 64 in 24 months), while `show_hn` returns thousands of mostly hobby projects. Use `--query` to download only the queries you want. Linked pages are not fetched; a document is the post text plus top-level comments.

### How the download scans a channel
The channel listing is processed **newest first**, with no cap by default: every video that passes Layer 0 (and is within `min_date`, default 24 months) is downloaded. The listing has no upload date, so the date is checked per video, and a channel stops after `stop_after_consecutive_old` (3) consecutive videos older than `min_date`. `max_videos` is optional per channel (omit it for no cap; `--max-videos N` overrides it for a run, handy for tests). Relevance is decided later by Layer 1, which is cheap.

### Other languages
Set `languages: [ja]` (or `[fr]`, `[es, en]`...) on a channel in `config/channels.yaml`. Captions are taken in that order; automatic captions are used only when the video's own language matches (YouTube lists machine translations for every video, so they are ignored). Layer 1 and extraction read non-English text directly and answer in English. The title regex rules in `config/filters.yaml` are English-only, so they do nothing for other languages.

## Behaviour

- **Resumable and cached.** Downloads skip existing files and permanent failures (`data/failures.jsonl`). Layer 1 results are cached in `data/triage/<video_id>.json`; changing thresholds never re-calls the API, only bumping `layer1.prompt_version` or the model does. Batches are resumed from `data/batches/`.
- **Layer 0** attributes each dropped video to the first stage that removed it (duration, then views percentile per channel, then title score) and reports how often each title rule matched, dropped and kept videos.
- **Batch vs `--sync`.** Batch is cheaper but can take up to an hour; `--sync` is immediate and full price, handy for small samples.
- **Cost safety.** `triage` prints a token and cost estimate first. With `VERIFICAR` placeholders in `pricing.yaml` the USD estimate is unavailable and `--yes` is refused.
- Output: `data/transcripts/<channel>/<video_id>.json`, `data/triage/`, `data/funnel/layer0_<channel>.json`, logs in `data/logs/`.
- `views_per_day` needs the upload date, which the fast yt-dlp listing does not provide; when missing it falls back to raw views (counted in the Layer 0 report).
