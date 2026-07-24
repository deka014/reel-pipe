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


