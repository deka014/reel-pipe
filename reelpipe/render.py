"""Single-pass ffmpeg rendering.

The old pipeline encoded every video three times: once to letterbox it, once to
split it, once to burn on the overlay. Here seek, scale, pad, overlay and encode
all happen in one invocation per clip, and clips render in parallel.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, textimage
from .config import Encoder, Series, TARGET_HEIGHT, TARGET_WIDTH
from .probe import ffmpeg_path


class RenderError(Exception):
    """An ffmpeg invocation failed."""


@dataclass
class RenderJob:
    clip_id: int
    source: Path
    output: Path
    start: float
    duration: float
    overlay_text: str
    series: Series
    # Set by render_all when the text has emoji or non-Latin script, which
    # drawtext's single font can't draw; the text is then stamped on as an image.
    overlay_image: Path | None = None
    overlay_y: int = 0


@dataclass
class RenderResult:
    clip_id: int
    output: Path
    ok: bool
    error: str = ""


def escape_filter_path(path: Path | str) -> str:
    r"""Make a Windows path safe inside an ffmpeg filter argument.

    Backslashes become forward slashes and the drive colon is escaped, so
    C:\Windows\Fonts\arialbd.ttf becomes C\:/Windows/Fonts/arialbd.ttf.
    """
    text = str(path).replace("\\", "/")
    return text.replace(":", r"\:")


def build_filter(overlay_text: str, series: Series, text_file: Path | None) -> str:
    """Scale to fit, letterbox to 1080x1920, then burn on the overlay."""
    chain = [
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}"
        ":force_original_aspect_ratio=decrease",
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
    ]

    if overlay_text.strip() and text_file is not None:
        # textfile= sidesteps drawtext's quoting rules for ':', '%', '\' and quotes,
        # and handles the multi-line overlays natively.
        chain.append(
            "drawtext="
            f"fontfile='{escape_filter_path(series.font)}'"
            f":textfile='{escape_filter_path(text_file)}'"
            ":fontcolor=" + series.font_color +
            f":fontsize={series.font_size}"
            f":borderw={series.border_width}"
            f":bordercolor={series.border_color}"
            ":line_spacing=10"
            ":text_align=center"
            ":x=(w-text_w)/2"
            f":y={series.text_y}"
        )

    return "[0:v]" + ",".join(chain) + "[v]"


def build_image_filter(overlay_y: int) -> str:
    """Scale and letterbox, then stamp the pre-drawn text image (input 1) on top.

    The image is a single frame; overlay keeps showing it for the whole clip.
    """
    return (
        f"[0:v]scale={TARGET_WIDTH}:{TARGET_HEIGHT}"
        ":force_original_aspect_ratio=decrease,"
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1[bg];"
        f"[bg][1:v]overlay=0:{overlay_y}:format=auto[v]"
    )


def build_command(job: RenderJob, enc: Encoder, text_file: Path | None) -> list[str]:
    """One ffmpeg call that does the whole job for a single clip."""
    if job.overlay_image is not None:
        extra_input = ["-i", str(job.overlay_image)]
        graph = build_image_filter(job.overlay_y)
    else:
        extra_input = []
        graph = build_filter(job.overlay_text, job.series, text_file)
    return [
        ffmpeg_path(),
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-y",
        # -ss before -i is a fast keyframe seek and still frame-accurate.
        "-ss", f"{job.start:.3f}",
        "-t", f"{job.duration:.3f}",
        "-i", str(job.source),
        *extra_input,
        "-filter_complex", graph,
        "-map", "[v]",
        # The '?' makes audio optional so silent sources render instead of failing.
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", enc.preset,
        "-crf", str(enc.crf),
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.1",
        "-r", str(enc.fps),
        "-g", str(enc.fps * 2),
        "-c:a", "aac",
        "-b:a", enc.audio_bitrate,
        "-ar", "44100",
        "-ac", "2",
        # Instagram wants the moov atom up front; without it the upload is re-encoded.
        "-movflags", "+faststart",
