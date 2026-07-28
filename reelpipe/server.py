"""The control panel: a local HTTP server with a JSON API and one HTML page.

Standard library only. It listens on 127.0.0.1, and every action that takes
time (prepare, post, login, doctor) runs the same CLI the terminal uses, as a
subprocess whose output is shown live. That keeps one code path for posting,
makes a job stoppable, and keeps Playwright out of the server's threads.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import config, fetch, schedule, state
from .config import ConfigError
from .runlock import Busy, post_lock
from .schedule import QueueError

HOST = "127.0.0.1"
UI_FILE = Path(__file__).resolve().parent / "ui.html"
TASK_NAME = "ReelPipe"
AUTO_KEY = "auto_post"
POST_KINDS = {"post_next", "post_ids", "post_due"}

FAILURE_NAME = re.compile(r"^clip\d{4}-[\w-]+\.png$")
TIME_FORMAT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind: str, title: str, args: list[str]):
        self.kind = kind
        self.title = title
        self.args = args
        self.proc: subprocess.Popen | None = None
        self.log: deque[str] = deque(maxlen=3000)
        self.started = datetime.now()
        self.ended: datetime | None = None
        self.exit_code: int | None = None
        self.stopped = False

    @property
    def running(self) -> bool:
        return self.ended is None

    def snapshot(self, tail: int = 400) -> dict:
        return {
            "kind": self.kind,
            "title": self.title,
            "running": self.running,
            "started": self.started.isoformat(timespec="seconds"),
            "ended": self.ended.isoformat(timespec="seconds") if self.ended else None,
            "exit_code": self.exit_code,
            "stopped": self.stopped,
            "log": list(self.log)[-tail:],
        }


def _python() -> str:
    """The console interpreter, even if the server itself runs under pythonw."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


class Jobs:
    """At most one job at a time; a short history of finished ones."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=15)

    def busy(self) -> bool:
        with self._lock:
            return self.current is not None and self.current.running

    def start(self, kind: str, title: str, args: list[str]) -> Job:
        with self._lock:
            if self.current is not None and self.current.running:
                raise ApiError(409, f"'{self.current.title}' is still running")
            job = Job(kind, title, args)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                job.proc = subprocess.Popen(
                    [_python(), "-u", "-m", "reelpipe.cli", *args],
                    cwd=str(config.CODE_ROOT),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
            except OSError as exc:
                raise ApiError(500, f"could not start: {exc}") from exc
            if self.current is not None:
                self.history.appendleft(self.current)
            self.current = job
            threading.Thread(target=self._pump, args=(job,), daemon=True).start()
            return job

    def _pump(self, job: Job) -> None:
        assert job.proc is not None and job.proc.stdout is not None
        for line in job.proc.stdout:
            job.log.append(line.rstrip("\r\n"))
        job.exit_code = job.proc.wait()
        if job.kind in POST_KINDS:
            # A killed run's lock lingers briefly after the process is gone.
            note = recover_interrupted(wait=15)
            if note:
                job.log.append(f"-- {note} --")
        job.ended = datetime.now()

    def stop(self) -> bool:
        with self._lock:
            job = self.current
            if job is None or not job.running or job.proc is None:
                return False
            job.stopped = True
            job.log.append("")
            job.log.append("-- stopped from the control panel --")
            pid = job.proc.pid
        # The CLI has children of its own (Playwright's driver, Chrome, ffmpeg),
        # so the whole tree has to go, not just the top process.
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            job.proc.terminate()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "current": self.current.snapshot() if self.current else None,
                "history": [j.snapshot(tail=60) for j in self.history],
            }


def recover_interrupted(wait: float = 0.0) -> str:
    """After a posting job ends, settle any clip it left mid-upload.

    Stopped before Share: back to the queue. Stopped after Share: parked as
    failed for a manual check, since it may be live. Returns a note for the
    job's log when there was something to report.
    """
    try:
        with post_lock(wait=wait):
            # Right after a posting process is killed, Windows can still be
            # releasing its handles on the database files, and opening the
            # database in that moment fails with a transient "disk I/O error".
            deadline = time.monotonic() + max(wait, 3.0)
            while True:
                try:
                    with state.session() as conn:
                        back = state.reset_stuck_posting(conn)
                        parked = state.park_stuck_sharing(conn)
                    break
                except sqlite3.OperationalError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.3)
    except Busy:
        return "another posting run holds the lock; it will tidy up interrupted clips itself"
    except Exception as exc:  # noqa: BLE001
        return f"could not tidy up interrupted clips: {exc}"
    notes = []
    if back:
        notes.append(f"{back} interrupted clip(s) returned to the queue")
    if parked:
        notes.append(f"{parked} clip(s) stopped after Share parked as failed; check the profile")
    return "; ".join(notes)


# --------------------------------------------------------------------- auto-poster

class AutoPoster(threading.Thread):
    """Starts a scheduled post when a slot comes due, while the panel is running."""

    def __init__(self, jobs: Jobs, interval: float = 15.0):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.interval = interval
        self.last_note = ""

    def due_now(self) -> bool:
        cfg = config.load()
        with state.session() as conn:
            if state.get_setting(conn, AUTO_KEY, "0") != "1":
                self.last_note = ""
                return False
            now = datetime.now()
            if not state.due_clips(conn, now, limit=1):
                self.last_note = ""
                return False
            last = state.last_posted_time(conn)
            gap = timedelta(minutes=cfg.pacing.min_gap_minutes)
            if last is not None and now - last < gap:
                resume = last + gap
                self.last_note = f"holding until {resume:%H:%M} (minimum gap after the last post)"
                return False
        self.last_note = ""
        return True

    def run(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                if not self.jobs.busy() and self.due_now():
                    self.jobs.start("post_due", "Scheduled post", ["post", "--due"])
            except Exception:
                # A broken config or a busy database must not kill the thread.
                continue


# ---------------------------------------------------------------- Windows task

class WindowsTask:
    """An optional scheduled task so posting continues with the panel closed."""

    def __init__(self) -> None:
        self._cached: bool | None = None
        self._checked = 0.0

    @staticmethod
    def supported() -> bool:
        return os.name == "nt" and not os.environ.get("REELPIPE_HOME")

    def installed(self) -> bool:
        if not self.supported():
            return False
        if self._cached is None or time.monotonic() - self._checked > 30:
            proc = subprocess.run(
                ["schtasks", "/Query", "/TN", TASK_NAME],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._cached = proc.returncode == 0
            self._checked = time.monotonic()
        return self._cached

    def install(self) -> None:
        if not self.supported():
            raise ApiError(400, "background task is only available on Windows")
        pythonw = Path(_python()).with_name("pythonw.exe")
        launcher = config.CODE_ROOT / "autopost.pyw"
        if not pythonw.exists() or not launcher.exists():
            raise ApiError(500, "pythonw.exe or autopost.pyw is missing")
        proc = subprocess.run(
            [
                "schtasks", "/Create", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", "10",
                "/TR", f'"{pythonw}" "{launcher}"',
                # Only when logged on: the upload needs a visible browser.
                "/IT", "/F",
            ],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._cached = None
        if proc.returncode != 0:
            raise ApiError(500, (proc.stderr or proc.stdout or "schtasks failed").strip())

    def remove(self) -> None:
        if not self.supported():
            return
        subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._cached = None


# -------------------------------------------------------------------------- app

class App:
    def __init__(self) -> None:
        self.jobs = Jobs()
        self.auto = AutoPoster(self.jobs)
        self.task = WindowsTask()
        self.thumb_gate = threading.Semaphore(3)
        # clip id -> source mtime for clips ffmpeg couldn't thumbnail, so a
        # broken file isn't re-tried every time the list redraws.
        self.thumb_failed: dict[int, float] = {}

    # ----------------------------------------------------------------- state

    def state(self) -> dict:
        out: dict = {
            "now": datetime.now().isoformat(timespec="seconds"),
            "jobs": self.jobs.snapshot(),
            "config_error": None,
        }
        try:
            cfg = config.load()
        except ConfigError as exc:
            out["config_error"] = str(exc)
            cfg = None

        with state.session() as conn:
            now = datetime.now()
            clips = []
            for r in state.all_clips(conn):
                shot = None
                if r["status"] == "failed" and r["last_failed_step"]:
                    name = f"clip{int(r['id']):04d}-{r['last_failed_step']}.png"
                    if (config.FAILURES / name).exists():
                        shot = name
                clips.append({
                    "id": r["id"], "video_id": r["video_id"],
                    "part_no": r["part_no"], "total_parts": r["total_parts"],
                    "duration_s": r["duration_s"], "status": r["status"],
                    "scheduled_for": r["scheduled_for"], "posted_at": r["posted_at"],
                    "attempts": r["attempts"], "last_error": r["last_error"],
                    "last_failed_step": r["last_failed_step"],
                    "caption": r["caption"], "overlay_text": r["overlay_text"],
                    "video_title": r["video_title"], "series_id": r["series_id"],
                    "has_file": bool(r["path"]), "failure_shot": shot,
                })
            videos = [
                {
                    "id": v["id"], "title": v["title"], "url": v["url"],
                    "youtube_id": v["youtube_id"], "series_id": v["series_id"],
                    "status": v["status"], "duration_s": v["duration_s"],
                }
                for v in state.all_videos(conn)
            ]
            known = {v["youtube_id"] for v in videos}
            out.update(
                clips=clips,
                videos=videos,
                counts=state.counts(conn),
                next_due=state.next_due_time(conn),
                overdue=state.overdue_count(conn, now),
                auto_post=state.get_setting(conn, AUTO_KEY, "0") == "1",
                auto_note=self.auto.last_note,
            )

        pending = []
        for entry in config.read_urls():
            yt = fetch.extract_id(entry.url)
            if yt is None or yt not in known:
                pending.append({"url": entry.url, "series_id": entry.series_id})
        out["pending_urls"] = pending

        if cfg is not None:
            p = cfg.pacing
            out["series"] = [
                {"id": s.id, "title": s.title, "default": s.default,
                 "part_seconds": s.part_seconds, "hashtags": len(s.hashtags)}
                for s in cfg.series
            ]
            out["pacing"] = {
                "post_times": [t.strftime("%H:%M") for t in p.post_times[: p.posts_per_day]],
                "clips_per_slot": p.clips_per_slot,
                "jitter_minutes": p.jitter_minutes,
                "min_gap_minutes": p.min_gap_minutes,
                "max_attempts": p.max_attempts,
                "batch_gap_seconds": list(p.batch_gap_seconds),
            }
            out["human"] = {"enabled": cfg.human.enabled}
        out["task"] = {
            "supported": self.task.supported(),
            "installed": self.task.installed(),
        }
        return out

    # --------------------------------------------------------------- actions

    @staticmethod
    def _ids(body: dict, key: str = "ids") -> list[int]:
        raw = body.get(key)
        if not isinstance(raw, list) or not raw:
            raise ApiError(400, f"'{key}' must be a non-empty list")
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError) as exc:
            raise ApiError(400, f"'{key}' must contain clip ids") from exc

    def queue_reorder(self, body: dict) -> dict:
        with state.session() as conn:
            schedule.reorder(conn, self._ids(body))
        return {}

    def clip_next(self, body: dict) -> dict:
        with state.session() as conn:
            schedule.make_next(conn, int(body.get("id", 0)))
        return {}

    def clips_skip(self, body: dict) -> dict:
        with state.session() as conn:
            return {"changed": schedule.skip(conn, self._ids(body))}

    def clips_unskip(self, body: dict) -> dict:
        cfg = config.load()
        with state.session() as conn:
            return {"changed": schedule.unskip(conn, self._ids(body), cfg.pacing)}

    def clips_requeue(self, body: dict) -> dict:
        with state.session() as conn:
            return {"changed": state.requeue(conn, self._ids(body))}

    def clips_mark_posted(self, body: dict) -> dict:
        with state.session() as conn:
            return {"changed": state.mark_posted_manual(conn, self._ids(body))}

    def clip_caption(self, body: dict) -> dict:
        caption = body.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            raise ApiError(400, "caption cannot be empty")
        if len(caption) > 2200:
            raise ApiError(400, "Instagram captions are limited to 2,200 characters")
        with state.session() as conn:
            if not state.set_caption(conn, int(body.get("id", 0)), caption.strip()):
                raise ApiError(409, "this clip can no longer be edited")
        return {}

    def clip_time(self, body: dict) -> dict:
        try:
            when = datetime.fromisoformat(str(body.get("when", "")))
        except ValueError as exc:
            raise ApiError(400, "not a valid date and time") from exc
        with state.session() as conn:
            schedule.set_time(conn, int(body.get("id", 0)), when)
        return {}

    def video_skip(self, body: dict) -> dict:
        with state.session() as conn:
            ids = state.video_clip_ids(conn, int(body.get("id", 0)), ("queued", "failed"))
            return {"changed": schedule.skip(conn, ids) if ids else 0}

    def video_unskip(self, body: dict) -> dict:
        cfg = config.load()
        with state.session() as conn:
            ids = state.video_clip_ids(conn, int(body.get("id", 0)), ("skipped",))
            return {"changed": schedule.unskip(conn, ids, cfg.pacing) if ids else 0}

    def video_remove(self, body: dict) -> dict:
        if self.jobs.busy():
            raise ApiError(409, "wait for the running job to finish first")
        video_id = int(body.get("id", 0))
        with state.session() as conn:
            video = state.get_video(conn, video_id)
            if video is None:
                raise ApiError(404, "no such video")
            clip_ids = [
                int(r["id"]) for r in
                conn.execute("SELECT id FROM clips WHERE video_id=?", (video_id,))
            ]
            try:
                paths = state.remove_video(conn, video_id)
            except RuntimeError as exc:
                raise ApiError(409, str(exc)) from exc
            url, source = video["url"], video["source_path"]

        # Files go only after the database change has committed, and only
        # from inside this project's own work folders.
        for raw in [*paths, source]:
            if not raw:
                continue
            target = Path(raw)
            try:
                inside = target.resolve().is_relative_to(config.WORK.resolve())
            except OSError:
                inside = False
            if inside:
                target.unlink(missing_ok=True)
        for clip_id in clip_ids:
            (config.THUMBS / f"{clip_id}.jpg").unlink(missing_ok=True)
        # Otherwise the next prepare would download it all over again.
        _drop_url(url)
        return {}

    def urls_add(self, body: dict) -> dict:
        cfg = config.load()
        series_id = str(body.get("series_id", "")).strip()
        try:
            series = cfg.series_for_id(series_id)
        except ConfigError as exc:
            raise ApiError(400, str(exc)) from exc

        lines = [ln.strip() for ln in str(body.get("urls", "")).splitlines() if ln.strip()]
        if not lines:
            raise ApiError(400, "paste at least one YouTube link")
        bad = [ln for ln in lines if fetch.extract_id(ln.split()[0]) is None]
        if bad:
            raise ApiError(400, "not a YouTube video link: " + bad[0][:80])

        with state.session() as conn:
            known = {v["youtube_id"] for v in state.all_videos(conn)}
        listed = {fetch.extract_id(e.url) for e in config.read_urls()}

        added, skipped = 0, 0
        new_lines = []
        for ln in lines:
            url = ln.split()[0]
            yt = fetch.extract_id(url)
            if yt in known or yt in listed:
                skipped += 1
                continue
            listed.add(yt)
            new_lines.append(f"{url} | {series.id}")
            added += 1
        if new_lines:
            existing = config.URLS_PATH.read_text(encoding="utf-8") if config.URLS_PATH.exists() else ""
            if existing and not existing.endswith("\n"):
                existing += "\n"
            config.URLS_PATH.write_text(existing + "\n".join(new_lines) + "\n", encoding="utf-8")
        return {"added": added, "skipped": skipped}

    def urls_remove(self, body: dict) -> dict:
        _drop_url(str(body.get("url", "")))
        return {}

    def reschedule(self, body: dict) -> dict:
        cfg = config.load()
        with state.session() as conn:
            return {"changed": schedule.reschedule(conn, cfg.pacing)}

    def settings(self, body: dict) -> dict:
        times = body.get("post_times")
        if not isinstance(times, list) or not times:
            raise ApiError(400, "add at least one post time")
        times = sorted({str(t) for t in times})
        for t in times:
            if not TIME_FORMAT.match(t):
                raise ApiError(400, f"'{t}' is not a time like 09:30")
        try:
            gap = [float(x) for x in body.get("batch_gap_seconds", [45, 150])]
            pacing = {
                "post_times": times,
                "posts_per_day": len(times),
                "clips_per_slot": int(body.get("clips_per_slot", 1)),
                "jitter_minutes": int(body.get("jitter_minutes", 20)),
                "min_gap_minutes": int(body.get("min_gap_minutes", 45)),
                "max_attempts": int(body.get("max_attempts", 3)),
                "batch_gap_seconds": gap,
            }
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "settings must be numbers") from exc
        try:
            config.update_toml("pacing", pacing)
            if "human_enabled" in body:
                config.update_toml("human", {"enabled": bool(body["human_enabled"])})
        except ConfigError as exc:
            raise ApiError(400, str(exc)) from exc

        changed = 0
        if body.get("reschedule"):
            cfg = config.load()
            with state.session() as conn:
                changed = schedule.reschedule(conn, cfg.pacing)
        return {"rescheduled": changed}

    def auto_set(self, body: dict) -> dict:
        with state.session() as conn:
            state.set_setting(conn, AUTO_KEY, "1" if body.get("enabled") else "0")
        return {}

    def task_set(self, body: dict) -> dict:
        if body.get("install"):
            self.task.install()
        else:
            self.task.remove()
        return {"installed": self.task.installed()}

    def job_start(self, body: dict) -> dict:
        kind = str(body.get("kind", ""))
        if kind == "prepare":
            job = self.jobs.start(kind, "Download & prepare", ["prepare"])
        elif kind == "login":
            job = self.jobs.start(kind, "Instagram login", ["login"])
        elif kind == "doctor":
            job = self.jobs.start(kind, "System check", ["doctor"])
        elif kind == "post_due":
            job = self.jobs.start(kind, "Post what's due", ["post", "--due"])
        elif kind == "post_next":
            n = int(body.get("n", 1))
            if not 1 <= n <= 10:
                raise ApiError(400, "choose between 1 and 10 clips")
            job = self.jobs.start(kind, f"Post next {n}", ["post", "--next", str(n)])
        elif kind == "post_ids":
            ids = self._ids(body)
            if len(ids) > 10:
                raise ApiError(400, "at most 10 clips in one go")
            if self.jobs.busy():
                raise ApiError(409, "another job is still running")
            with state.session() as conn:
                # Failed clips picked for posting go back in the queue first.
                state.requeue(conn, ids)
            job = self.jobs.start(
                kind, f"Post {len(ids)} selected",
                ["post", "--ids", ",".join(str(i) for i in ids)],
            )
        else:
            raise ApiError(400, f"unknown job '{kind}'")
        return {"job": job.snapshot(tail=5)}

    def job_stop(self, body: dict) -> dict:
        return {"stopped": self.jobs.stop()}

    # ----------------------------------------------------------------- media

    def clip_file(self, clip_id: int) -> Path:
        with state.session() as conn:
            row = conn.execute("SELECT path FROM clips WHERE id=?", (clip_id,)).fetchone()
        if row is None or not row["path"]:
            raise ApiError(404, "no file for this clip")
        path = Path(row["path"])
        try:
            inside = path.resolve().is_relative_to(config.CLIPS.resolve())
        except OSError:
            inside = False
        if not inside or not path.is_file():
            raise ApiError(404, "clip file is missing")
        return path

    def thumb_file(self, clip_id: int) -> Path:
        thumb = config.THUMBS / f"{clip_id}.jpg"
        source = self.clip_file(clip_id)
        mtime = source.stat().st_mtime
        if thumb.exists() and thumb.stat().st_mtime >= mtime:
            return thumb
        if self.thumb_failed.get(clip_id) == mtime:
            raise ApiError(404, "could not make a thumbnail")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ApiError(404, "ffmpeg not available for thumbnails")
        config.THUMBS.mkdir(parents=True, exist_ok=True)
        with self.thumb_gate:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "1",
                 "-i", str(source), "-frames:v", "1", "-vf", "scale=180:320",
                 "-q:v", "5", str(thumb)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if not thumb.exists():
            self.thumb_failed[clip_id] = mtime
            raise ApiError(404, "could not make a thumbnail")
        return thumb


def _drop_url(url: str) -> None:
    """Remove a video's line from urls.txt, leaving comments and the rest alone."""
    if not url or not config.URLS_PATH.exists():
        return
    target = fetch.extract_id(url)
    kept = []
    for line in config.URLS_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            first = stripped.replace("|", " ").split()[0]
            if first == url or (target and fetch.extract_id(first) == target):
                continue
        kept.append(line)
    config.URLS_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")


# ------------------------------------------------------------------------- http

POST_ROUTES = {
    "/api/queue/reorder": "queue_reorder",
    "/api/clips/next": "clip_next",
    "/api/clips/skip": "clips_skip",
    "/api/clips/unskip": "clips_unskip",
    "/api/clips/requeue": "clips_requeue",
    "/api/clips/mark-posted": "clips_mark_posted",
    "/api/clips/caption": "clip_caption",
    "/api/clips/time": "clip_time",
    "/api/videos/skip": "video_skip",
    "/api/videos/unskip": "video_unskip",
    "/api/videos/remove": "video_remove",
    "/api/urls/add": "urls_add",
    "/api/urls/remove": "urls_remove",
    "/api/reschedule": "reschedule",
    "/api/settings": "settings",
    "/api/auto": "auto_set",
    "/api/task": "task_set",
    "/api/job/start": "job_start",
    "/api/job/stop": "job_stop",
}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReelPipe"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

        # -- guards

        def _local(self) -> bool:
            """Only answer requests addressed to this machine by name.

            Stops another website from reaching the panel by pointing a domain
            of its own at 127.0.0.1.
            """
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        # -- responses

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _file(self, path: Path, cache: bool = False) -> None:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                first, _, last = rng[6:].split(",")[0].partition("-")
                try:
                    if first:
                        start = int(first)
                        end = int(last) if last else size - 1
                    else:
                        start = max(0, size - int(last))
                except ValueError:
                    start, end = 0, size - 1
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            try:
                with path.open("rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = fh.read(min(256 * 1024, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (ConnectionError, OSError):
                pass  # the browser stopped listening, e.g. seeking in a video

        # -- routes

        def do_GET(self) -> None:
            if not self._local():
                self._json(403, {"error": "forbidden"})
                return
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self._json(200, app.state())
                elif m := re.fullmatch(r"/media/clip/(\d+)", path):
                    self._file(app.clip_file(int(m.group(1))))
                elif m := re.fullmatch(r"/media/thumb/(\d+)", path):
                    self._file(app.thumb_file(int(m.group(1))), cache=True)
                elif m := re.fullmatch(r"/media/failure/([^/]+)", path):
                    name = m.group(1)
                    target = config.FAILURES / name
                    if not FAILURE_NAME.match(name) or not target.is_file():
                        raise ApiError(404, "no such screenshot")
                    self._file(target)
                else:
                    self._json(404, {"error": "not found"})
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except (ConnectionError, BrokenPipeError):
                pass
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_POST(self) -> None:
            if not self._local():
                self._json(403, {"error": "forbidden"})
                return
            # A custom header can't be sent cross-site without a preflight this
            # server never grants, so other pages can't trigger actions here.
            if self.headers.get("X-ReelPipe") != "1":
                self._json(403, {"error": "forbidden"})
                return
            path = urlparse(self.path).path
            name = POST_ROUTES.get(path)
            if name is None:
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_000_000:
                    raise ApiError(413, "request too large")
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ApiError(400, "body must be JSON") from exc
                if not isinstance(body, dict):
                    raise ApiError(400, "body must be a JSON object")
                result = getattr(app, name)(body)
                self._json(200, {"ok": True, **result})
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except QueueError as exc:
                self._json(409, {"error": str(exc)})
            except ConfigError as exc:
                self._json(400, {"error": str(exc)})
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": f"bad request: {exc}"})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def create(port: int) -> tuple[ThreadingHTTPServer, App]:
    app = App()
    httpd = ThreadingHTTPServer((HOST, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd, app


def run(port: int = 8765, open_browser: bool = True) -> int:
    try:
        httpd, app = create(port)
    except OSError:
        print(f"Port {port} is in use. The control panel may already be running:")
        print(f"  http://{HOST}:{port}/")
        print("Otherwise pick another port:  reelpipe ui --port 8766")
        return 1

    recover_interrupted()
    app.auto.start()
    url = f"http://{HOST}:{port}/"
    print(f"ReelPipe control panel: {url}")
    print("Leave this window open while you use it. Ctrl+C stops it.")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        if app.jobs.busy():
            print("A job is still running and will finish on its own.")
    finally:
        httpd.server_close()
    return 0
