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
            f"No config file at {path}. Copy config.example.toml to config.toml and edit it."
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    series_raw = raw.get("series", [])
    if not series_raw:
        raise ConfigError("config.toml defines no [[series]] blocks.")
    series = [_series_from_toml(i, s) for i, s in enumerate(series_raw)]

    seen: dict[str, str] = {}
    for s in series:
        key = s.id.casefold()
        if key in seen:
            raise ConfigError(
                f"Duplicate series id {s.id!r} (ids are matched without case, "
                f"so it clashes with {seen[key]!r})."
            )
        seen[key] = s.id

    defaults = [s.id for s in series if s.default]
    if len(defaults) > 1:
        raise ConfigError(
            "More than one [[series]] is marked `default = true`: "
            + ", ".join(defaults)
        )

    p = raw.get("pacing", {})
    times = [_parse_time(t) for t in p.get("post_times", ["09:30", "14:00", "19:30"])]
    if not times:
        raise ConfigError("pacing.post_times must list at least one \"HH:MM\" time.")
    pacing = Pacing(
        posts_per_day=int(p.get("posts_per_day", len(times))),
        post_times=sorted(times),
        jitter_minutes=int(p.get("jitter_minutes", 20)),
        min_gap_minutes=int(p.get("min_gap_minutes", 45)),
        max_attempts=int(p.get("max_attempts", 3)),
        clips_per_slot=int(p.get("clips_per_slot", 1)),
        batch_gap_seconds=_range(
            "pacing", "batch_gap_seconds", p.get("batch_gap_seconds"), (45.0, 150.0)
        ),
    )
    if pacing.posts_per_day > len(times):
        raise ConfigError(
            f"pacing.posts_per_day is {pacing.posts_per_day} but only "
            f"{len(times)} post_times are defined."
        )
    if pacing.posts_per_day < 1:
        raise ConfigError("pacing.posts_per_day must be at least 1.")
    if not 1 <= pacing.clips_per_slot <= 10:
        raise ConfigError("pacing.clips_per_slot must be between 1 and 10.")
    if pacing.jitter_minutes < 0 or pacing.min_gap_minutes < 0:
        raise ConfigError("pacing.jitter_minutes and min_gap_minutes cannot be negative.")
    if pacing.max_attempts < 1:
        raise ConfigError("pacing.max_attempts must be at least 1.")

    e = raw.get("encoder", {})
    encoder = Encoder(
        preset=e.get("preset", "veryfast"),
        crf=int(e.get("crf", 20)),
        fps=int(e.get("fps", 30)),
        audio_bitrate=e.get("audio_bitrate", "128k"),
        workers=int(e.get("workers", 4)),
        threads_per_job=int(e.get("threads_per_job", 4)),
    )

    human = _human_from_toml(raw.get("human", {}))

    return Config(series=series, pacing=pacing, encoder=encoder, human=human)


def _range(section: str, key: str, raw, default: tuple[float, float]) -> tuple[float, float]:
    """A [min, max] pair, or a single number meaning min == max."""
    value = raw if raw is not None else list(default)
    if isinstance(value, (int, float)):
        value = [value, value]
    if not (isinstance(value, list) and len(value) == 2):
        raise ConfigError(f"{section}.{key} must be a number or [min, max].")
    lo, hi = float(value[0]), float(value[1])
    if lo < 0 or lo > hi:
        raise ConfigError(f"{section}.{key} must be non-negative with min <= max.")
    return lo, hi


def _human_from_toml(h: dict) -> HumanConfig:
    known = {f for f in HumanConfig.__dataclass_fields__}
    unknown = set(h) - known
    if unknown:
        raise ConfigError(f"[human] has unknown key(s): {', '.join(sorted(unknown))}")

    d = HumanConfig()
    cfg = HumanConfig(
        enabled=bool(h.get("enabled", d.enabled)),
        browse_seconds=_range("human", "browse_seconds", h.get("browse_seconds"), d.browse_seconds),
        typing_wpm=_range("human", "typing_wpm", h.get("typing_wpm"), d.typing_wpm),
        typo_rate=float(h.get("typo_rate", d.typo_rate)),
        think_pause_rate=float(h.get("think_pause_rate", d.think_pause_rate)),
        linger_seconds=_range("human", "linger_seconds", h.get("linger_seconds"), d.linger_seconds),
    )
    if cfg.typing_wpm[0] < 5:
        raise ConfigError("human.typing_wpm must be at least 5.")
    for key in ("typo_rate", "think_pause_rate"):
        rate = getattr(cfg, key)
        if not 0 <= rate <= 0.3:
            raise ConfigError(f"human.{key} must be between 0 and 0.3.")
    return cfg


def ensure_dirs() -> None:
    for d in (WORK, DOWNLOADS, CLIPS, PROFILE, FAILURES, THUMBS, LOGS):
        d.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------ editing config.toml

def toml_literal(value) -> str:
    """Format a bool, number, string or flat list as a TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to config.toml")


def update_toml(section: str, values: dict, path: Path | None = None) -> None:
    """Change single-line keys in one [section] of config.toml, in place.

    Comments, ordering and every other section are left exactly as they were,
    which a parse-and-rewrite could not do. The result is loaded again before
    it is kept; if it doesn't validate, the file is put back and the error
    raised.
    """
    path = path or CONFIG_PATH
    # Bytes in and out, so the file keeps whichever line endings it already has.
    original = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()

    header = re.compile(r"^\s*\[\[?[^\]]+\]\]?\s*(#.*)?$")
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip() == f"[{section}]"), None
    )
    if start is None:
        lines += ["", f"[{section}]"]
        start = len(lines) - 1
    end = next(
        (i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines)
    )

    for key, value in values.items():
        literal = toml_literal(value)
        key_re = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(.*)$")
        for i in range(start + 1, end):
            m = key_re.match(lines[i])
            if not m:
                continue
            old = m.group(2)
            if old.count("[") != old.count("]"):
                raise ConfigError(
                    f"{section}.{key} spans several lines in config.toml; "
                    "edit it there by hand."
                )
            lines[i] = m.group(1) + literal
            break
        else:
            # Not present yet: add it after the section's last non-blank line.
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, f"{key} = {literal}")
            end += 1

    path.write_bytes((eol.join(lines) + eol).encode("utf-8"))
    try:
        load(path)
    except Exception:
        path.write_bytes(original.encode("utf-8"))
        raise


def read_urls(path: Path | None = None) -> list[UrlEntry]:
    """Read urls.txt: one `URL | series-id` per line.

    The separator may be a pipe or plain whitespace, since a URL contains
    neither. Blank lines and lines starting with # are ignored. A line with no
    id falls back to the series marked `default = true`.
    """
    path = path or URLS_PATH
    if not path.exists():
        return []

    entries: list[UrlEntry] = []
    seen: set[str] = set()

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if "|" in line:
            url, _, series_id = line.partition("|")
        else:
            parts = line.split(None, 1)
            url, series_id = parts[0], (parts[1] if len(parts) > 1 else "")

        url = url.strip()
        series_id = series_id.strip()
        if not url:
            continue

        # Same URL twice is a no-op anyway, but skipping it keeps output honest.
        if url in seen:
            continue
        seen.add(url)
        entries.append(UrlEntry(url=url, series_id=series_id, line_no=line_no))

    return entries
