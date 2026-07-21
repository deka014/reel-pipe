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
        "-threads", str(enc.threads_per_job),
        str(job.output),
    ]


def render_one(job: RenderJob, enc: Encoder) -> RenderResult:
    """Render a single clip. Never raises -- failures come back on the result."""
    job.output.parent.mkdir(parents=True, exist_ok=True)
    text_file: Path | None = None

    try:
        if job.overlay_text.strip() and job.overlay_image is None:
            # newline="\n": on Windows the default turns each line break into
            # CR+LF, and drawtext treats the CR as a second line break, which
            # put a blank line between every pair of overlay lines.
            fd = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8", newline="\n"
            )
            with fd:
                fd.write(job.overlay_text)
            text_file = Path(fd.name)

        cmd = build_command(job, enc, text_file)
        proc = subprocess.run(cmd, capture_output=True, text=True)

        if proc.returncode != 0:
            return RenderResult(
                job.clip_id,
                job.output,
                ok=False,
                error=(proc.stderr or "ffmpeg failed with no output").strip()[-1200:],
            )

        if not job.output.exists() or job.output.stat().st_size == 0:
            return RenderResult(
                job.clip_id, job.output, ok=False,
                error="ffmpeg reported success but produced no usable file",
            )

        return RenderResult(job.clip_id, job.output, ok=True)

    except Exception as exc:  # noqa: BLE001 - reported per clip, never fatal
        return RenderResult(job.clip_id, job.output, ok=False, error=str(exc))
    finally:
        if text_file is not None:
            text_file.unlink(missing_ok=True)


def render_all(
    jobs: list[RenderJob],
    enc: Encoder,
    on_done: Callable[[RenderResult], None] | None = None,
) -> list[RenderResult]:
    """Render clips in parallel.

    Each ffmpeg gets threads_per_job threads; workers * threads_per_job is sized
    to the machine's core count.
    """
    if not jobs:
        return []

    results: list[RenderResult] = []

    # Overlays with emoji or non-Latin script are drawn by a browser first, all
    # in one go, before the encodes start.
    image_dir: Path | None = None
    fancy = [
        j for j in jobs
        if j.overlay_image is None and j.overlay_text.strip()
        and textimage.needs_image(j.overlay_text)
    ]
    if fancy:
        image_dir = Path(tempfile.mkdtemp(prefix="reelpipe_overlay_"))
        try:
            images = textimage.render(
                [(j.clip_id, j.overlay_text, j.series) for j in fancy], image_dir
            )
            for j in fancy:
                j.overlay_image = images[j.clip_id].path
                j.overlay_y = images[j.clip_id].y
        except Exception as exc:  # noqa: BLE001
            # Falling back to drawtext would burn empty boxes into the video
            # where the emoji should be, and a posted clip can't be fixed. So
            # these clips fail instead, and stay pending for the next prepare.
            for j in fancy:
                result = RenderResult(
                    j.clip_id, j.output, ok=False,
                    error="could not draw the overlay text (it has emoji or "
                          f"non-Latin characters, which need the browser): {exc}",
                )
                results.append(result)
                if on_done:
                    on_done(result)
            jobs = [j for j in jobs if j not in fancy]

    try:
        if jobs:
            workers = max(1, min(enc.workers, len(jobs)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(render_one, job, enc): job for job in jobs}
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    if on_done:
                        on_done(result)
    finally:
        if image_dir is not None:
            shutil.rmtree(image_dir, ignore_errors=True)

    return results


def clip_filename(youtube_id: str, part_no: int, total: int) -> Path:
    return config.CLIPS / f"{youtube_id}_part{part_no:02d}of{total:02d}.mp4"


def benchmark(sample: Path, enc: Encoder) -> float:
    """Encode 10 seconds of a real file and return the wall time, for `doctor`."""
    import time

    out = Path(tempfile.gettempdir()) / "reelpipe_bench.mp4"
    job = RenderJob(
        clip_id=0,
        source=sample,
        output=out,
        start=0.0,
        duration=10.0,
        overlay_text="Benchmark\nPart 1",
        series=Series(id="bench", title="bench"),
    )
    started = time.perf_counter()
    result = render_one(job, enc)
    elapsed = time.perf_counter() - started
    out.unlink(missing_ok=True)
    if not result.ok:
        raise RenderError(result.error)
    return elapsed
