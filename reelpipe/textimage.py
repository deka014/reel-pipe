"""Draw overlay text to a transparent PNG with a headless browser.

ffmpeg's drawtext uses exactly one font file and cannot fall back to another,
so any character the chosen font lacks -- every emoji, and scripts such as
Devanagari when the font is Arial -- comes out as an empty box. A browser's
text engine does font fallback, colour emoji and complex-script shaping
properly, so overlays that need any of that are drawn here and then stamped
onto the video as an image.

Plain ASCII overlays don't come through here; drawtext handles those fine.
"""

from __future__ import annotations

import base64
import html
import math
from dataclasses import dataclass
from pathlib import Path

from .config import Series, TARGET_WIDTH

# Room around the text for the outline, and side margins so long lines wrap
# before reaching the edge of the frame.
PAD_Y = 24
PAD_X = 40
# drawtext adds 10px between lines; a font's natural line height is ~1.15em.
LINE_HEIGHT = "calc(1.15em + 10px)"
# Fallbacks after the configured font: colour emoji, then Indic scripts.
FALLBACK_FONTS = '"Segoe UI Emoji", "Noto Color Emoji", "Nirmala UI", "Segoe UI", sans-serif'


class TextImageError(Exception):
    """The browser could not be started or the text could not be drawn."""


@dataclass
class OverlayImage:
    path: Path
    # Where the image's top edge goes so the text lands at the series' text_y.
    y: int


def needs_image(text: str) -> bool:
    """True when the text has anything drawtext's single font can't be trusted with."""
    return any(ord(ch) > 127 for ch in text)


def css_color(color: str) -> str:
    """Translate an ffmpeg colour ('white', '0xRRGGBB', 'black@0.85') to CSS."""
    base, _, alpha = color.strip().partition("@")
    base = base.strip()
    if base.lower().startswith("0x"):
        base = "#" + base[2:]
    if not alpha:
        return base
    try:
        opacity = max(0.0, min(1.0, float(alpha)))
    except ValueError:
        return base
    return f"color-mix(in srgb, {base} {opacity * 100:.0f}%, transparent)"


def _outline(width: int, color: str) -> str:
    """An outline built from a ring of shadows.

    Unlike a text stroke, shadows also follow the shape of colour emoji, so the
    emoji gets the same dark edge as the letters.
    """
    if width <= 0:
        return "none"
    points = 20
    return ", ".join(
        f"{width * math.cos(2 * math.pi * i / points):.2f}px "
        f"{width * math.sin(2 * math.pi * i / points):.2f}px 0 {color}"
        for i in range(points)
    )


_font_cache: dict[str, str] = {}


def _font_data_uri(font: str) -> str:
    """The configured font, inlined, so the page needs no file access."""
    if font not in _font_cache:
        raw = Path(font).read_bytes()
        _font_cache[font] = "data:font/ttf;base64," + base64.b64encode(raw).decode("ascii")
    return _font_cache[font]


def build_html(text: str, series: Series) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
/* The weight range stops the browser from thickening the configured font,
   while bold below still selects the bold face of any fallback font. */
@font-face {{ font-family: "overlay"; font-weight: 100 900; src: url("{_font_data_uri(series.font)}"); }}
html, body {{ margin: 0; background: transparent; }}
#b {{ display: inline-block; width: 0; height: 0; }}
#t {{
  width: {TARGET_WIDTH}px; box-sizing: border-box; padding: {PAD_Y}px {PAD_X}px;
  text-align: center; white-space: pre-wrap; overflow-wrap: anywhere;
  font-family: "overlay", {FALLBACK_FONTS}; font-weight: 700;
  font-size: {series.font_size}px; line-height: {LINE_HEIGHT};
  color: {css_color(series.font_color)};
  text-shadow: {_outline(series.border_width, css_color(series.border_color))};
}}
</style></head><body><div id="t"><span id="b"></span>{html.escape(text)}</div></body></html>"""


# Where the top of the first line's letters falls inside the image.
#
# drawtext puts the top of the lettering at text_y, so the image has to be
# placed by its lettering too, not by its edge. #b is an empty inline-block at
# the start of the text: its bottom edge sits on the first line's baseline in
# the real layout, including any extra height a fallback font adds. From the
# baseline, the configured font's own ink height gives the top of the letters.
MEASURE_JS = """(firstLine) => {
  const box = document.getElementById('t');
  const mark = document.getElementById('b');
  const baseline = mark.getBoundingClientRect().bottom - box.getBoundingClientRect().top;
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = '700 ' + getComputedStyle(box).fontSize + ' overlay';
  const letters = firstLine.replace(/[^ -~]/g, '').trim() || 'H';
  return baseline - ctx.measureText(letters).actualBoundingBoxAscent;
}"""


def render(items: list[tuple[int, str, Series]], out_dir: Path) -> dict[int, OverlayImage]:
    """Draw each (key, text, series) to `out_dir/<key>.png`. One browser for all."""
    if not items:
        return {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - playwright is a hard dependency
        raise TextImageError(f"playwright is not installed: {exc}") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    images: dict[int, OverlayImage] = {}
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception:
                # No bundled Chromium; the installed Chrome draws text just as well.
                browser = pw.chromium.launch(headless=True, channel="chrome")
            page = browser.new_page(
                viewport={"width": TARGET_WIDTH, "height": 800}, device_scale_factor=1
            )
            for key, text, series in items:
                page.set_content(build_html(text, series))
                # The inlined font loads asynchronously; a screenshot taken
                # before it's ready would use the fallback font instead.
                page.evaluate("document.fonts.ready.then(() => true)")
                ink_top = page.evaluate(MEASURE_JS, text.split("\n")[0])
                target = out_dir / f"{key}.png"
                page.locator("#t").screenshot(path=str(target), omit_background=True)
                images[key] = OverlayImage(target, series.text_y - round(ink_top))
            browser.close()
    except TextImageError:
        raise
    except Exception as exc:
        raise TextImageError(str(exc).splitlines()[0] if str(exc) else type(exc).__name__) from exc
    return images
