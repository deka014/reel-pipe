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
            self.page.mouse.move(bx, by)
            self.wait_ms(step_ms)
        self.page.mouse.move(x, y)
        self.x, self.y = x, y

    def click(self, locator: Locator) -> None:
        """Move to the element and click it, near the middle but not dead centre."""
        if not self.enabled:
            locator.click()
            return

        box = locator.bounding_box()
        if box is None or not self._in_viewport(box):
            locator.scroll_into_view_if_needed()
            self.wait_ms(self.rng.uniform(250, 600))
            box = locator.bounding_box()
        if box is None:
            locator.click()
            return

        fx = _clamp(self.rng.gauss(0.5, 0.14), 0.2, 0.8)
        fy = _clamp(self.rng.gauss(0.5, 0.14), 0.25, 0.75)
        tx = box["x"] + box["width"] * fx
        ty = box["y"] + box["height"] * fy
        self.move_to(tx, ty)

        # If something else sits on top of the target (a popover, a toast), a raw
        # mouse click would land on that instead; let Playwright's checked click
        # handle it and report clearly.
        if not self._hits(locator, self.x, self.y):
            locator.click()
            return

        self.wait_ms(self.rng.uniform(80, 350))  # hover before pressing
        self.page.mouse.down()
        self.wait_ms(self.rng.uniform(55, 130))
        self.page.mouse.up()

    def _in_viewport(self, box: dict) -> bool:
        return (
            box["y"] >= 0
            and box["x"] >= 0
            and box["y"] + box["height"] <= self.vh
            and box["x"] + box["width"] <= self.vw
        )

    @staticmethod
    def _hits(locator: Locator, x: float, y: float) -> bool:
        try:
            return bool(
                locator.evaluate(
                    "(el, p) => { const h = document.elementFromPoint(p.x, p.y);"
                    " return !!h && (el === h || el.contains(h)); }",
                    {"x": x, "y": y},
                )
            )
        except Exception:
            return False

    # ------------------------------------------------------------------ scrolling

    def browse_feed(self, seconds: float) -> None:
        """Scroll the feed the way people do: a flick of the wheel, then a look."""
        if not self.enabled or seconds <= 0:
            return
        # The wheel scrolls whatever is under the pointer, so rest it on the feed.
        self.move_to(
            self.vw * self.rng.uniform(0.4, 0.6),
            self.vh * self.rng.uniform(0.35, 0.65),
        )
        deadline = time.monotonic() + seconds * self.time_scale
        while time.monotonic() < deadline:
            # Occasionally scroll back up to look at something again.
            direction = -1 if self.rng.random() < 0.12 else 1
            for _ in range(self.rng.randint(2, 6)):
                self.page.mouse.wheel(0, direction * self.rng.uniform(80, 130))
                self.wait_ms(self.rng.uniform(25, 70))
            self.pause(0.8, 3.5)

    # ------------------------------------------------------------------ typing

    def type_text(self, text: str, allow_typos: bool = True) -> None:
        """Type into the focused element with an uneven, human rhythm.

        Hashtags and mentions open a suggestion list; pressing Enter while it is
        open would pick a suggestion instead of starting a new line, so a space
        closes the token before any newline and at the very end.
        """
        kb = self.page.keyboard
        wpm = self.rng.uniform(*self.cfg.typing_wpm)
        base_ms = 60_000 / (wpm * 5)  # a "word" is five characters by convention
        typo_rate = self.cfg.typo_rate if (allow_typos and self.enabled) else 0.0

        token = ""
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]

            if ch == "\n":
                token = self._close_token(token)
                kb.press("Enter")
                self._key_delay(base_ms, ch)
                i += 1
                continue

            if ord(ch) > 127:
                # Emoji and non-Latin script come from a picker or an IME in real
                # life, arriving as a unit rather than keystroke by keystroke.
                j = i
                while j < n and ord(text[j]) > 127:
                    j += 1
                self.pause(0.3, 1.2)
                kb.insert_text(text[i:j])
                token += text[i:j]
                self._key_delay(base_ms, text[j - 1])
                i = j
                continue

            lower = ch.lower()
            if typo_rate and lower in NEIGHBOURS and self.rng.random() < typo_rate:
                wrong = self.rng.choice(NEIGHBOURS[lower])
                kb.type(wrong.upper() if ch.isupper() else wrong)
                self.wait_ms(self.rng.uniform(120, 420))  # noticing the slip
                kb.press("Backspace")
                self.wait_ms(self.rng.uniform(70, 220))

            kb.type(ch)
            token = "" if ch == " " else token + ch
            self._key_delay(base_ms, ch)

            if ch == " " and self.rng.random() < self.cfg.think_pause_rate:
                self.pause(0.8, 2.6)
            i += 1

        self._close_token(token)

    def _close_token(self, token: str) -> str:
        if len(token) > 1 and token[0] in "#@":
            self.page.keyboard.type(" ")
            self.wait_ms(self.rng.uniform(120, 300))
        return ""

    def _key_delay(self, base_ms: float, ch: str) -> None:
        if not self.enabled:
            return
        delay = base_ms * self.rng.lognormvariate(0, 0.35)
        if ch == "\n":
            delay += self.rng.uniform(400, 1400)
        elif ch in PUNCTUATION:
            delay += self.rng.uniform(150, 500)
        elif ch == " ":
            delay += self.rng.uniform(0, 90)
        self.wait_ms(delay)
