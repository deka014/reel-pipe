"""ffprobe wrapper and part-boundary planning."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProbeError(Exception):
    """ffprobe is missing or the file could not be read."""


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool
    fps: float


@dataclass
class Part:
    """One planned clip: where it starts in the source and how long it runs."""

    part_no: int
    start: float
    duration: float


def ffprobe_path() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise ProbeError(
            "ffprobe not found on PATH. Install with: winget install --id Gyan.FFmpeg"
        )
    return exe


def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ProbeError(
            "ffmpeg not found on PATH. Install with: winget install --id Gyan.FFmpeg"
        )
    return exe


def _parse_fps(raw: str | None) -> float:
    """r_frame_rate arrives as a rational like '30000/1001'."""
    if not raw:
        return 0.0
    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(raw)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path) -> MediaInfo:
    """Read duration, dimensions, fps and audio presence in a single ffprobe call."""
    if not path.exists():
        raise ProbeError(f"File not found: {path}")

    cmd = [
        ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path.name}: {proc.stderr.strip()}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"Could not parse ffprobe output for {path.name}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProbeError(f"No video stream in {path.name}")

    # The container duration is more reliable than the stream's for trimmed files.
    duration = 0.0
    for candidate in (data.get("format", {}).get("duration"), video.get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    if duration <= 0:
        raise ProbeError(f"Could not determine duration of {path.name}")

    return MediaInfo(
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
