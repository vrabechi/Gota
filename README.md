# Gota

Pipeline to find online business models that work in the US but are still underdeveloped in Brazil.

- **Stage 1 (done):** download YouTube transcripts.
- Stage 2 (pending approval): structured business extraction with the Anthropic API.
- Stage 3 (pending): scored ranking report.

## Setup

```
uv sync
```

## Stage 1: download transcripts

Edit `config/channels.yaml` (channels, `max_videos`, `min_date`, optional `title_keywords`), then:

```
uv run python src/download.py --channel starter-story --max-videos 3   # small test
uv run python src/download.py                                          # all channels
```

Useful flags: `--dry-run` (list only), `--retry-failed`, `--cookies-from-browser chrome` (if YouTube blocks requests).

- Output: `data/transcripts/<channel>/<video_id>.json`
- Re-running skips videos already saved and videos that failed permanently (e.g. no captions). Failures are logged in `data/failures.jsonl`; the full log is `data/logs/download.log`.
- Candidates are the newest `scan_limit` videos of each channel, taken in descending view-count order, keeping only those newer than `min_date` (default: last 24 months).
- Captions: manual English first, then auto-generated (original language track), then `youtube-transcript-api` as fallback.
