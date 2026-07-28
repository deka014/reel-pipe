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
