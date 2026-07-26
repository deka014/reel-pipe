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
            state.mark_video(conn, int(row["id"]), "split")
            conn.commit()
            audio = "" if info.has_audio else " [no audio]"
            _print(
                f"  [ok] [{series.id}] {row['title'][:44]}: {total} part(s) "
                f"of <= {series.part_seconds}s{audio}"
            )

        # 4. Render everything still pending, in parallel.
        pending = state.clips_needing_render(conn)
        if not pending:
            _print("\nNothing to render.")
        else:
            _print(f"\nRendering {len(pending)} clip(s) "
                   f"({cfg.encoder.workers} at a time)...")
            jobs = []
            for row in pending:
                series = cfg.series_for_id(row["series_id"])
                jobs.append(
                    RenderJob(
                        clip_id=int(row["id"]),
                        source=Path(row["source_path"]),
                        output=render.clip_filename(
                            row["youtube_id"], int(row["part_no"]),
                            int(row["total_parts"]),
                        ),
                        start=float(row["start_s"]),
                        duration=float(row["duration_s"]),
                        overlay_text=row["overlay_text"],
                        series=series,
                    )
                )

            started = time.perf_counter()
            done = [0]

            def report(result: render.RenderResult) -> None:
                done[0] += 1
                tag = "ok" if result.ok else "fail"
                _print(f"  [{tag}] ({done[0]}/{len(jobs)}) {result.output.name}")

            results = render.render_all(jobs, cfg.encoder, on_done=report)
            elapsed = time.perf_counter() - started

            for result in results:
                if result.ok:
                    state.mark_rendered(conn, result.clip_id, result.output)
                else:
                    state.mark_clip_error(conn, result.clip_id, "render", result.error)
            conn.commit()

            good = sum(1 for r in results if r.ok)
            _print(f"\nRendered {good}/{len(results)} clip(s) in {elapsed:.1f}s.")
            for result in results:
                if not result.ok:
                    _print(f"  error on clip {result.clip_id}: {result.error[:300]}")

        # 5. Hand out schedule slots.
        assigned = schedule.assign_slots(conn, cfg.pacing)
        conn.commit()
        if assigned:
            _print(f"Queued {assigned} clip(s) on the drip schedule.")
        nxt = state.next_due_time(conn)
        if nxt:
            _print(f"Next post due: {nxt}")

    return 0


# ------------------------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> int:
    with state.session() as conn:
        counts = state.counts(conn)
        _print("Queue")
        _print("=" * 52)
        for status in state.CLIP_STATUSES:
            _print(f"  {status:<10} {counts[status]:>5}")
        total = sum(counts.values())
        _print(f"  {'total':<10} {total:>5}")

        nxt = state.next_due_time(conn)
        _print(f"\nNext due: {nxt or '-- nothing queued --'}")

        upcoming = conn.execute(
            "SELECT c.scheduled_for, c.part_no, c.total_parts, v.title "
            "FROM clips c JOIN videos v ON v.id=c.video_id "
            "WHERE c.status='queued' ORDER BY c.scheduled_for LIMIT 10"
        ).fetchall()
        if upcoming:
            _print("\nUpcoming:")
            for row in upcoming:
                _print(
                    f"  {row['scheduled_for']}  "
                    f"part {row['part_no']}/{row['total_parts']}  "
                    f"{(row['title'] or '')[:40]}"
                )

        failures = state.failed_clips(conn)
        if failures:
            _print(f"\nFailed ({len(failures)}):")
            for row in failures:
                _print(
                    f"  clip {row['id']} part {row['part_no']}  "
                    f"step={row['last_failed_step']}  "
                    f"{(row['last_error'] or '')[:80]}"
                )
            _print("  Fix the selector, then run: reelpipe retry")
    return 0


# --------------------------------------------------------------------------- show

def _table(rows: list, columns: list[str], max_width: int = 44) -> None:
    """Print rows as an aligned table, truncating long cells."""
    if not rows:
        _print("  (no rows)")
        return

    def cell(row, col: str) -> str:
        value = row[col]
        if value is None:
            return "-"
        text = str(value).replace("\n", " / ")
        return text if len(text) <= max_width else text[: max_width - 1] + "…"

    widths = {
        c: max(len(c), max((len(cell(r, c)) for r in rows), default=0))
        for c in columns
    }
    _print("  " + "  ".join(c.ljust(widths[c]) for c in columns))
    _print("  " + "  ".join("-" * widths[c] for c in columns))
    for row in rows:
        _print("  " + "  ".join(cell(row, c).ljust(widths[c]) for c in columns))


def cmd_show(args: argparse.Namespace) -> int:
    try:
        conn = state.connect_readonly()
    except FileNotFoundError:
        _print(f"No database yet at {config.DB_PATH}. Run `reelpipe prepare` first.")
        return 1

    try:
        # Arbitrary read-only query, for anything the presets don't cover.
        if args.sql:
            try:
                rows = conn.execute(args.sql).fetchall()
            except sqlite3.Error as exc:
                _print(f"SQL error: {exc}")
                return 1
            if not rows:
                _print("  (no rows)")
                return 0
            _table(rows, list(rows[0].keys()), max_width=args.width)
            _print(f"\n  {len(rows)} row(s)")
            return 0

        # One clip in full, including the untruncated caption.
        if args.clip is not None:
            row = conn.execute(
                "SELECT c.*, v.title AS video_title, v.youtube_id, v.series_id "
                "FROM clips c JOIN videos v ON v.id=c.video_id WHERE c.id=?",
                (args.clip,),
            ).fetchone()
            if row is None:
                _print(f"No clip with id {args.clip}.")
                return 1
            _print(f"Clip {row['id']}")
            _print("=" * 52)
            for key in row.keys():
                if key == "caption":
                    continue
                _print(f"  {key:<17} {row[key]}")
            _print("\n  caption:")
            for line in (row["caption"] or "").splitlines():
                _print(f"    {line}")
            return 0

        what = args.table or "all"

        if what in ("all", "videos"):
            rows = conn.execute(
                "SELECT id, youtube_id, series_id, status, "
                "       ROUND(duration_s) AS secs, title "
                "FROM videos ORDER BY id"
            ).fetchall()
            _print(f"videos ({len(rows)})")
            _table(rows, ["id", "youtube_id", "series_id", "status", "secs", "title"],
                   args.width)
            _print("")

        if what in ("all", "clips"):
            sql = (
                "SELECT c.id, c.video_id AS vid, c.part_no AS part, "
                "       c.total_parts AS of, ROUND(c.duration_s) AS secs, "
                "       c.status, c.scheduled_for, c.attempts AS tries, "
                "       c.last_failed_step AS failed_at "
                "FROM clips c"
            )
            params: list = []
            if args.status:
                sql += " WHERE c.status = ?"
                params.append(args.status)
            sql += " ORDER BY c.video_id, c.part_no"
            rows = conn.execute(sql, params).fetchall()
            label = f"clips ({len(rows)})" + (f" status={args.status}" if args.status else "")
            _print(label)
            _table(rows, ["id", "vid", "part", "of", "secs", "status",
                          "scheduled_for", "tries", "failed_at"], args.width)
            _print("")

        if what in ("all", "hashtags"):
            rows = conn.execute(
                "SELECT id, clip_id, tag_set_hash, used_at "
                "FROM hashtag_history ORDER BY id DESC LIMIT 10"
            ).fetchall()
            _print(f"hashtag_history (last {len(rows)})")
            _table(rows, ["id", "clip_id", "tag_set_hash", "used_at"], args.width)

        if what == "all":
            _print("\nTip: `reelpipe show --clip <id>` for one clip's full caption,")
            _print("     `reelpipe show --sql \"SELECT ...\"` for anything else.")
    finally:
        conn.close()

    return 0


# -------------------------------------------------------------------------- login

def cmd_login(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    _print("Opening Chrome with the ReelPipe profile.")
    _print("Log into Instagram by hand in that window; the session is then reused.")
    _print("Nothing is typed for you and no credentials are stored by this tool.\n")

    with upload.browser(headless=False) as session_:
        page = session_.page
        upload.open_instagram(page)

        if upload.is_logged_in(page):
            _print("Already logged in -- nothing to do.")
            return 0

        _print("Waiting for you to finish logging in (up to 5 minutes)...")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if upload.is_logged_in(page):
                upload.dismiss_dialogs(page)
                page.wait_for_timeout(1_500)
                _print("\nLogged in. Session saved to the profile directory.")
                return 0
            page.wait_for_timeout(2_000)

        _print("\nTimed out waiting for login.")
        return 1


# --------------------------------------------------------------------------- post

