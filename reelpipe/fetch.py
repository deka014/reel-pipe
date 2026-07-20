"""yt-dlp wrapper: resolve YouTube URLs to local mp4 files."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config


class FetchError(Exception):
    """yt-dlp is missing, or a download failed."""


@dataclass
class Fetched:
    youtube_id: str
    title: str
    duration: float
    path: Path


# Accepts watch?v=, youtu.be/, /shorts/, /embed/ and bare 11-char ids.
_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"^([A-Za-z0-9_-]{11})$"),
)

# Prefer an mp4/m4a pair so the merge is a remux rather than a re-encode.
FORMAT_SPEC = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b"


def ytdlp_path() -> str:
    exe = shutil.which("yt-dlp")
    if not exe:
        raise FetchError(
            "yt-dlp not found on PATH. Install with: winget install --id yt-dlp.yt-dlp"
        )
    return exe


def extract_id(url: str) -> str | None:
    """Pull the video id out of a URL without a network call, when possible."""
    for pattern in _ID_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def resolve_id(url: str) -> str:
    """Get the video id, falling back to asking yt-dlp for unusual URL shapes."""
    local = extract_id(url)
    if local:
        return local
    proc = subprocess.run(
        [ytdlp_path(), "--no-playlist", "--skip-download", "--print", "%(id)s", url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise FetchError(f"Could not resolve a video id from {url}: {proc.stderr.strip()}")
    return proc.stdout.strip().splitlines()[0]


def existing_download(youtube_id: str) -> Path | None:
    """Find an already-downloaded file for this id, whatever extension it landed with."""
    for candidate in sorted(config.DOWNLOADS.glob(f"{youtube_id}.*")):
        if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}:
            return candidate
    return None


def download(url: str) -> Fetched:
    """Download one video, skipping the transfer if the file is already on disk."""
    config.DOWNLOADS.mkdir(parents=True, exist_ok=True)
    out_template = str(config.DOWNLOADS / "%(id)s.%(ext)s")

    cmd = [
        ytdlp_path(),
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--newline",
        "-f", FORMAT_SPEC,
        "--merge-output-format", "mp4",
        "-o", out_template,
        # Reuse the file if it is already there rather than downloading again.
        "--no-overwrites",
        # One JSON blob per video gives us id, title and duration without a second call.
        "--print-json",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FetchError(f"yt-dlp failed for {url}:\n{proc.stderr.strip()[-1500:]}")

    info = _parse_info(proc.stdout)
    if info is None:
        raise FetchError(f"yt-dlp produced no metadata for {url}")

    youtube_id = str(info.get("id") or "")
    title = str(info.get("title") or youtube_id)
    try:
        duration = float(info.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    path = existing_download(youtube_id)
    if path is None:
        # Fall back to the path yt-dlp reported if the glob missed it.
        reported = info.get("_filename") or info.get("filename")
        if reported and Path(reported).exists():
            path = Path(reported)
    if path is None:
        raise FetchError(
            f"yt-dlp reported success for {youtube_id} but no output file was found "
            f"in {config.DOWNLOADS}"
        )

    return Fetched(youtube_id=youtube_id, title=title, duration=duration, path=path)


def _parse_info(stdout: str) -> dict | None:
    """Take the last JSON object from yt-dlp's output.

    Progress lines and warnings can share stdout, so scan rather than assume.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None
