"""Helpers to turn raw caption files into clean plain text."""
from __future__ import annotations

import html
import re

_CUE_TIMING_RE = re.compile(r"^(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+")
_INLINE_TAG_RE = re.compile(r"<[^>]+>")
_SOUND_CUE_RE = re.compile(r"^\[[^\]]*\]$|^\([^)]*\)$")
_SPEAKER_CHANGE_RE = re.compile(r"^(>>+\s*)+")
_WHITESPACE_RE = re.compile(r"\s+")

# Auto-captions repeat the previous line(s) inside the next cue ("rolling" captions).
# Comparing against the last few emitted lines removes those repeats.
_DEDUP_WINDOW = 4


def _clean_line(line: str) -> str:
    line = _INLINE_TAG_RE.sub("", line)
    line = html.unescape(line)
    line = _SPEAKER_CHANGE_RE.sub("", line.strip())
    return _WHITESPACE_RE.sub(" ", line).strip()


def clean_vtt(raw: str) -> str:
    """Convert WebVTT (manual or auto-generated) into a single paragraph of text."""
    lines = raw.replace("\r\n", "\n").split("\n")
    emitted: list[str] = []
    in_header = True

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _CUE_TIMING_RE.match(stripped):
            in_header = False
            continue
        if in_header:
            continue  # WEBVTT, Kind:, Language:, STYLE blocks before the first cue
        # Cue identifier: a line directly followed by a timing line.
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if _CUE_TIMING_RE.match(next_line):
            continue

        text = _clean_line(stripped)
        if not text or _SOUND_CUE_RE.match(text):
            continue
        if text in emitted[-_DEDUP_WINDOW:]:
            continue
        emitted.append(text)

    return _WHITESPACE_RE.sub(" ", " ".join(emitted)).strip()


def clean_plain(text: str) -> str:
    """Normalise already-plain transcript text (e.g. from youtube-transcript-api)."""
    text = html.unescape(text)
    text = re.sub(r"\[[^\]]*\]", " ", text)  # [Music], [Applause]
    return _WHITESPACE_RE.sub(" ", text).strip()
