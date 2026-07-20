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
