"""Command line entry points."""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import captions, config, fetch, probe, render, schedule, state, upload
from .config import ConfigError
from .human import Human
from .runlock import Busy, post_lock
from .fetch import FetchError
from .probe import ProbeError
from .render import RenderJob


def _print(msg: str = "") -> None:
    # Instagram titles and captions can carry characters the console codec rejects.
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ------------------------------------------------------------------------- doctor

def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    _print("ReelPipe environment check")
    _print("=" * 52)

    for tool, hint in (
        ("ffmpeg", "winget install --id Gyan.FFmpeg"),
        ("ffprobe", "winget install --id Gyan.FFmpeg"),
        ("yt-dlp", "winget install --id yt-dlp.yt-dlp"),
    ):
        path = shutil.which(tool)
        if path:
            _print(f"  [ok]   {tool:<8} {path}")
        else:
            ok = False
            _print(f"  [MISS] {tool:<8} not on PATH -- {hint}")

    try:
        import playwright  # noqa: F401
        _print("  [ok]   playwright installed")
    except ImportError:
        ok = False
        _print("  [MISS] playwright -- pip install playwright")

    try:
        cfg = config.load()
        _print(f"  [ok]   config.toml ({len(cfg.series)} series defined)")
        for s in cfg.series:
            flag = "  (default)" if s.default else ""
            _print(
                f"         - id={s.id!r} parts={s.part_seconds}s "
                f"tags={len(s.hashtags)}{flag}"
            )
        _print(
            f"  [ok]   pacing: {cfg.pacing.posts_per_day}/day at "
            + ", ".join(t.strftime("%H:%M") for t in cfg.pacing.post_times)
        )
    except ConfigError as exc:
        ok = False
        cfg = None
        _print(f"  [MISS] config: {exc}")

    # Check every urls.txt line names a series that exists, before any download.
    urls = config.read_urls()
    if cfg is None:
        _print(f"  [ok]   urls.txt: {len(urls)} url(s) (ids unchecked -- fix config first)")
    else:
        problems: list[str] = []
        per_series: dict[str, int] = {}
        for entry in urls:
            try:
                series = cfg.series_for_id(entry.series_id)
                per_series[series.id] = per_series.get(series.id, 0) + 1
            except ConfigError as exc:
                problems.append(f"line {entry.line_no}: {exc}")
        if problems:
            ok = False
            _print(f"  [MISS] urls.txt: {len(problems)} line(s) name an unknown series")
            for p in problems[:10]:
                _print(f"         {p}")
        else:
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(per_series.items()))
            _print(
                f"  [ok]   urls.txt: {len(urls)} url(s)"
                + (f" ({breakdown})" if breakdown else "")
            )

    if config.PROFILE.exists() and any(config.PROFILE.iterdir()):
        _print("  [ok]   browser profile exists (run `login` if posting fails)")
    else:
        _print("  [note] no browser profile yet -- run `reelpipe login` before posting")

    # A real encode tells you the actual speed on this machine.
    if ok and cfg is not None and not args.no_benchmark:
        sample = _benchmark_sample()
        if sample:
            try:
                elapsed = render.benchmark(sample, cfg.encoder)
                _print(
                    f"  [ok]   encode benchmark: 10s clip in {elapsed:.1f}s "
                    f"({10 / elapsed:.1f}x realtime, single job)"
                )
            except Exception as exc:
                _print(f"  [warn] benchmark failed: {exc}")
            finally:
                sample.unlink(missing_ok=True)

    _print("=" * 52)
    _print("All good." if ok else "Fix the items marked MISS above.")
    return 0 if ok else 1


def _benchmark_sample() -> Path | None:
    """Synthesise a short clip so the benchmark needs no real footage."""
    import subprocess
    import tempfile

    out = Path(tempfile.gettempdir()) / "reelpipe_sample.mp4"
    cmd = [
        probe.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return out if proc.returncode == 0 and out.exists() else None
    except Exception:
        return None


# ------------------------------------------------------------------------ prepare

def cmd_prepare(args: argparse.Namespace) -> int:
    cfg = config.load()
    config.ensure_dirs()

    urls = config.read_urls()

    with state.session() as conn:
        # An empty urls.txt is only a problem if there's no work already queued up.
        if not urls:
            outstanding = (
                len(state.videos_needing_download(conn))
                + len(state.videos_needing_split(conn))
                + len(state.clips_needing_render(conn))
                + len(state.clips_needing_schedule(conn))
            )
            if not outstanding:
                queued = state.counts(conn)
                if sum(queued.values()):
                    _print(
                        f"Nothing to prepare -- {queued['queued']} clip(s) already "
                        f"queued, {queued['posted']} posted."
                    )
                    nxt = state.next_due_time(conn)
                    if nxt:
                        _print(f"Next post due: {nxt}")
                    return 0
                _print(
                    f"No URLs in {config.URLS_PATH} and nothing pending. "
                    "Add YouTube links, one per line."
                )
                return 1
            _print(f"No new URLs; resuming {outstanding} pending item(s).")

        # 1. Register every URL against the series named beside it in urls.txt.
        #    A bad id is caught here, before anything is downloaded.
        new = 0
        bad = 0
        for entry in urls:
            try:
                series = cfg.series_for_id(entry.series_id)
            except ConfigError as exc:
                bad += 1
                _print(f"  [skip] urls.txt line {entry.line_no}: {exc}")
                continue
            try:
                yt_id = fetch.resolve_id(entry.url)
            except FetchError as exc:
                bad += 1
                _print(f"  [skip] {entry.url}: {exc}")
                continue
            before = state.video_by_youtube_id(conn, yt_id)
            state.upsert_video(conn, entry.url, yt_id, series.id)
            if before is None:
                new += 1
        conn.commit()
        if urls:
            _print(
                f"Registered {len(urls) - bad}/{len(urls)} url(s); {new} new."
                + (f" {bad} skipped." if bad else "")
            )
            if bad:
                _print("  Fix the lines above and re-run; nothing else is affected.")

        # 2. Download whatever hasn't been fetched yet.
        todo = state.videos_needing_download(conn)
        if todo:
            _print(f"\nDownloading {len(todo)} video(s)...")
        for row in todo:
            try:
                _print(f"  fetching {row['youtube_id']}...")
                got = fetch.download(row["url"])
                state.mark_downloaded(conn, int(row["id"]), got.title, got.duration, got.path)
                conn.commit()
                _print(f"  [ok] {got.title[:60]} ({got.duration:.0f}s)")
            except FetchError as exc:
                state.mark_video(conn, int(row["id"]), "failed")
                conn.commit()
                _print(f"  [fail] {row['youtube_id']}: {exc}")

        # 3. Plan parts for anything downloaded but not yet split.
        to_split = state.videos_needing_split(conn)
        if to_split:
            _print(f"\nPlanning parts for {len(to_split)} video(s)...")
        for row in to_split:
            try:
                series = cfg.series_for_id(row["series_id"])
            except ConfigError as exc:
                _print(f"  [skip] {row['title'][:50]}: {exc}")
                continue

            source = Path(row["source_path"])
            try:
                info = probe.probe(source)
            except ProbeError as exc:
                _print(f"  [fail] {source.name}: {exc}")
                continue

            parts = probe.plan_parts(
                info.duration, series.part_seconds, series.min_tail_seconds
            )
            total = len(parts)
            for part in parts:
                overlay = captions.overlay_text(series, part.part_no, total)
                clip_id = state.add_clip(
                    conn, int(row["id"]), part.part_no, total,
                    part.start, part.duration, overlay, "",
                )
                caption = captions.caption_for_clip(
                    conn, clip_id, series, part.part_no, total
                )
                conn.execute(
                    "UPDATE clips SET caption=? WHERE id=? AND caption=''",
                    (caption, clip_id),
                )
