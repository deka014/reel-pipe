"""Load and validate config.toml, and resolve which series a video belongs to."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

# Where the code lives; subprocesses are started from here.
CODE_ROOT = Path(__file__).resolve().parent.parent

# Project root is the directory containing config.toml. REELPIPE_HOME points it
# somewhere else, which the tests use so they never touch real data.
ROOT = Path(os.environ["REELPIPE_HOME"]).resolve() if os.environ.get("REELPIPE_HOME") else CODE_ROOT

WORK = ROOT / "work"
DOWNLOADS = WORK / "downloads"
CLIPS = WORK / "clips"
PROFILE = WORK / "profile"
FAILURES = WORK / "failures"
THUMBS = WORK / "thumbs"
LOGS = WORK / "logs"
DB_PATH = ROOT / "state.db"
CONFIG_PATH = ROOT / "config.toml"
URLS_PATH = ROOT / "urls.txt"

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920


class ConfigError(Exception):
    """config.toml is missing or malformed."""


@dataclass
class Series:
    """A group of videos sharing overlay text, caption template and hashtags.

    Picked explicitly per URL in urls.txt by `id`, never guessed from the title.
    """

    id: str
    title: str
    overlay: str = "Part {part}"
    caption: str = "Part {part} of {title}"
    credit: str = ""
    part_seconds: int = 40
    min_tail_seconds: int = 8
    hashtag_count: tuple[int, int] = (10, 15)
    hashtags: list[str] = field(default_factory=list)
    # Used when a urls.txt line names no series. At most one may set this.
    default: bool = False
    # Overlay appearance
    font: str = r"C:\Windows\Fonts\arialbd.ttf"
    font_size: int = 64
    font_color: str = "white"
    border_width: int = 4
    border_color: str = "black@0.85"
    text_y: int = 400


@dataclass
class UrlEntry:
    """One line of urls.txt: a URL and the series id chosen for it."""

    url: str
    series_id: str
    line_no: int


@dataclass
class Pacing:
    posts_per_day: int = 3
    post_times: list[dtime] = field(default_factory=list)
    jitter_minutes: int = 20
    min_gap_minutes: int = 45
    max_attempts: int = 3
    # How many clips go out back to back at each post time.
    clips_per_slot: int = 1
    # Pause between two uploads in the same session, in seconds.
    batch_gap_seconds: tuple[float, float] = (45.0, 150.0)


@dataclass
class Encoder:
    preset: str = "veryfast"
    crf: int = 20
    fps: int = 30
    audio_bitrate: str = "128k"
    # Parallel ffmpeg processes; each gets threads_per_job threads.
    workers: int = 4
    threads_per_job: int = 4


@dataclass
class HumanConfig:
    """How the browser is driven: pacing of scrolling, mouse and typing."""

    enabled: bool = True
    # Scroll the feed for this long before starting a post.
    browse_seconds: tuple[float, float] = (10.0, 30.0)
    # Typing speed range; one speed is picked per caption.
    typing_wpm: tuple[float, float] = (40.0, 70.0)
    # Chance per letter of hitting a neighbouring key and correcting it.
    typo_rate: float = 0.02
    # Chance per word of a longer pause mid-caption.
    think_pause_rate: float = 0.06
    # Stay on the page this long after the post is confirmed.
    linger_seconds: tuple[float, float] = (4.0, 12.0)


@dataclass
class Config:
    series: list[Series]
    pacing: Pacing
    encoder: Encoder
    human: HumanConfig = field(default_factory=HumanConfig)

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.series]

    @property
    def default_series(self) -> Series | None:
        return next((s for s in self.series if s.default), None)

    def series_for_id(self, series_id: str) -> Series:
        """Look up a series by the id given in urls.txt. Matching ignores case."""
        wanted = (series_id or "").strip().casefold()

        if not wanted:
            fallback = self.default_series
            if fallback is not None:
                return fallback
            raise ConfigError(
                "This URL names no series, and no [[series]] is marked "
                "`default = true`. Either add an id after the URL in urls.txt "
                f"(one of: {', '.join(self.ids)}) or mark one series as the default."
            )

        for s in self.series:
            if s.id.casefold() == wanted:
                return s

        raise ConfigError(
            f"Unknown series id {series_id!r}. "
            f"config.toml defines: {', '.join(self.ids)}"
        )


def _parse_time(raw: str) -> dtime:
    try:
        hh, mm = raw.split(":")
        return dtime(int(hh), int(mm))
    except Exception as exc:
        raise ConfigError(f"Bad post_times entry {raw!r}; expected \"HH:MM\".") from exc


def _series_from_toml(idx: int, raw: dict) -> Series:
    if "id" not in raw:
        raise ConfigError(
            f"[[series]] #{idx + 1} is missing required key 'id'. "
            'Give it a short name or number, e.g. id = "ninja" or id = 1.'
        )
    if "title" not in raw:
        raise ConfigError(f"[[series]] #{idx + 1} is missing required key 'title'.")

    known = {f.name for f in Series.__dataclass_fields__.values()}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(
            f"[[series]] {raw['id']!r} has unknown key(s): {', '.join(sorted(unknown))}"
        )

    data = dict(raw)
    # TOML gives an int for `id = 1`; ids are compared as text.
    data["id"] = str(data["id"]).strip()
    if not data["id"]:
        raise ConfigError(f"[[series]] #{idx + 1} has an empty id.")
    if any(ch.isspace() for ch in data["id"]) or "|" in data["id"]:
        raise ConfigError(
            f"[[series]] id {data['id']!r} cannot contain spaces or '|', "
            "since urls.txt separates the URL from the id with those."
        )
    sid = data["id"]

    count = data.get("hashtag_count", [10, 15])
    if isinstance(count, int):
        count = [count, count]
    if not (isinstance(count, list) and len(count) == 2):
        raise ConfigError(
            f"[[series]] {sid!r}: hashtag_count must be an int or [min, max]."
        )
    lo, hi = int(count[0]), int(count[1])
    if lo > hi:
        raise ConfigError(f"[[series]] {sid!r}: hashtag_count min exceeds max.")
    tags = data.get("hashtags", [])
    if hi > len(tags):
        raise ConfigError(
            f"[[series]] {sid!r}: hashtag_count max is {hi} but only "
            f"{len(tags)} hashtags are defined."
        )
    data["hashtag_count"] = (lo, hi)

    if int(data.get("part_seconds", 40)) < 5:
        raise ConfigError(f"[[series]] {sid!r}: part_seconds must be >= 5.")

    font = Path(data.get("font", Series.font))
    if not font.exists():
        raise ConfigError(
            f"[[series]] {sid!r}: font not found at {font}. "
            r"Try C:\Windows\Fonts\arialbd.ttf"
        )

    return Series(**data)


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        raise ConfigError(
