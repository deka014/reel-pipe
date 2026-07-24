"""Human-paced browser interaction.

Playwright's defaults act like a script: the pointer teleports to the exact
centre of an element, text arrives at a fixed rate, and nothing ever pauses.
This module drives the same page the way a person would: the pointer travels
along a curved path and lands somewhere near the middle of its target, the
feed gets scrolled in wheel flicks with pauses to look, and captions are typed
with an uneven rhythm, slowing at punctuation, occasionally stopping to think,
and now and then hitting a neighbouring key and correcting it.

With `enabled = false` in config every method still performs the action, just
immediately, which is handy when debugging selectors.
"""

from __future__ import annotations

import math
import random
import time

from playwright.sync_api import Locator, Page

from .config import HumanConfig

# Neighbouring keys on a QWERTY layout, for realistic slips.
NEIGHBOURS: dict[str, str] = {
    "q": "wa", "w": "qes", "e": "wrd", "r": "etf", "t": "ryg", "y": "tuh",
    "u": "yij", "i": "uok", "o": "ipl", "p": "ol",
    "a": "qsz", "s": "awdz", "d": "sefx", "f": "drgc", "g": "fthv",
    "h": "gjyb", "j": "hkun", "k": "jlim", "l": "kop",
    "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn",
    "n": "bhjm", "m": "njk",
}

PUNCTUATION = set(".,!?;:")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    u = 1 - t
    return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3


class Human:
    """Wraps a page and performs pointer, wheel and keyboard actions at human pace."""

    def __init__(
        self,
        page: Page,
        cfg: HumanConfig,
        rng: random.Random | None = None,
        time_scale: float = 1.0,
    ):
        self.page = page
        self.cfg = cfg
        self.rng = rng or random.Random()
        # Shrinks every pause; only the tests set this.
        self.time_scale = time_scale

        viewport = page.viewport_size or {"width": 1280, "height": 900}
        self.vw = viewport["width"]
        self.vh = viewport["height"]
        # Playwright doesn't report where the pointer is, so track it here.
        self.x = self.rng.uniform(self.vw * 0.3, self.vw * 0.7)
        self.y = self.rng.uniform(self.vh * 0.3, self.vh * 0.7)
        page.mouse.move(self.x, self.y)

    # ------------------------------------------------------------------ timing

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def wait_ms(self, ms: float) -> None:
        if self.enabled and ms > 0:
            self.page.wait_for_timeout(ms * self.time_scale)

    def pause(self, lo: float, hi: float) -> None:
        """Wait a random number of seconds. Long waits drift the pointer slightly,
        since a hand resting on a mouse is never perfectly still."""
        if not self.enabled:
            return
        total_ms = self.rng.uniform(lo, hi) * 1000
        if total_ms > 1500 and self.rng.random() < 0.5:
            first = total_ms * self.rng.uniform(0.3, 0.7)
            self.wait_ms(first)
            self._drift()
            self.wait_ms(total_ms - first)
        else:
            self.wait_ms(total_ms)

    def _drift(self) -> None:
        dx = self.rng.uniform(-25, 25)
        dy = self.rng.uniform(-18, 18)
        self._curve(
            _clamp(self.x + dx, 5, self.vw - 5),
            _clamp(self.y + dy, 5, self.vh - 5),
            fast=True,
        )

    # ------------------------------------------------------------------ pointer

    def move_to(self, x: float, y: float) -> None:
        x = _clamp(x, 1, self.vw - 2)
        y = _clamp(y, 1, self.vh - 2)
        if not self.enabled:
            self.page.mouse.move(x, y)
            self.x, self.y = x, y
            return

        dist = math.hypot(x - self.x, y - self.y)
        if dist < 2:
            return
        # Long, fast moves sometimes overshoot and come back.
        if dist > 300 and self.rng.random() < 0.25:
            ux, uy = (x - self.x) / dist, (y - self.y) / dist
            over = self.rng.uniform(8, 24)
            self._curve(
                _clamp(x + ux * over, 1, self.vw - 2),
                _clamp(y + uy * over, 1, self.vh - 2),
            )
            self.wait_ms(self.rng.uniform(60, 160))
        self._curve(x, y)

    def _curve(self, x: float, y: float, fast: bool = False) -> None:
        """Travel along a cubic Bezier with eased speed and a little hand tremor."""
        sx, sy = self.x, self.y
        dx, dy = x - sx, y - sy
        dist = math.hypot(dx, dy)
        if dist < 1:
            return

        # Unit vector perpendicular to the straight line, to bend the path.
        px, py = -dy / dist, dx / dist
        bend1 = self.rng.uniform(-0.25, 0.25) * dist
        bend2 = self.rng.uniform(-0.25, 0.25) * dist
        c1x, c1y = sx + dx * 0.3 + px * bend1, sy + dy * 0.3 + py * bend1
        c2x, c2y = sx + dx * 0.7 + px * bend2, sy + dy * 0.7 + py * bend2

        steps = int(_clamp(dist / 14, 6 if fast else 12, 70))
        # Longer moves take longer, but not proportionally (roughly Fitts' law).
        duration = _clamp(180 + 90 * math.log2(1 + dist / 40), 150, 900)
        if fast:
            duration *= 0.5
        duration *= self.rng.uniform(0.85, 1.2)
        step_ms = duration / steps

        for i in range(1, steps):
            t = i / steps
            eased = t * t * (3 - 2 * t)  # slow start, quick middle, slow finish
            bx = _bezier(sx, c1x, c2x, x, eased) + self.rng.gauss(0, 0.6)
            by = _bezier(sy, c1y, c2y, y, eased) + self.rng.gauss(0, 0.6)
