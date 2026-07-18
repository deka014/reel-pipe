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


