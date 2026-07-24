"""Overlay text and caption/hashtag generation."""

from __future__ import annotations

import hashlib
import random
import sqlite3

from . import state
from .config import Series


def _fields(series: Series, part: int, total: int) -> dict[str, str]:
    return {
        "part": str(part),
        "total": str(total),
        "title": series.title,
        "credit": series.credit,
    }


def _fill(template: str, values: dict[str, str]) -> str:
    """Substitute {part}/{total}/{title}/{credit}, leaving unknown braces alone."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def overlay_text(series: Series, part: int, total: int) -> str:
    """Text burned into the video. Literal \\n in config becomes a real newline."""
    text = _fill(series.overlay, _fields(series, part, total))
    return text.replace("\\n", "\n")


def tag_hash(tags: list[str]) -> str:
    """Order-insensitive fingerprint of a hashtag set."""
    joined = " ".join(sorted(tags))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def pick_hashtags(
    series: Series,
    recent: set[str],
    rng: random.Random | None = None,
) -> tuple[list[str], str]:
    """Sample hashtags, avoiding sets recently used.

    Instagram penalises identical repeated captions, which plain random.sample
    could produce by chance on a small tag pool.
    """
    rng = rng or random
    if not series.hashtags:
        return [], tag_hash([])

    lo, hi = series.hashtag_count
    hi = min(hi, len(series.hashtags))
    lo = min(lo, hi)

    chosen: list[str] = []
    digest = ""
    for _ in range(12):
        count = rng.randint(lo, hi)
        chosen = rng.sample(series.hashtags, count)
        digest = tag_hash(chosen)
        if digest not in recent:
            break
    return chosen, digest


def build_caption(
    series: Series,
    part: int,
    total: int,
    recent: set[str],
    rng: random.Random | None = None,
) -> tuple[str, str]:
    """Return the full caption text and the fingerprint of its hashtag set."""
    base = _fill(series.caption, _fields(series, part, total)).replace("\\n", "\n")
    tags, digest = pick_hashtags(series, recent, rng)
    if not tags:
        return base, digest
    return f"{base}\n\n{' '.join(tags)}", digest


def caption_for_clip(
    conn: sqlite3.Connection,
    clip_id: int,
    series: Series,
    part: int,
    total: int,
    rng: random.Random | None = None,
) -> str:
    """Build a caption and record its hashtag set so the next one differs."""
    recent = state.recent_tag_hashes(conn, limit=3)
    caption, digest = build_caption(series, part, total, recent, rng)
    state.record_tag_hash(conn, clip_id, digest)
    return caption
