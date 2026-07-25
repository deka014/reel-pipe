"""Playwright uploader.

Every step waits on the element itself rather than sleeping, resolves against
an ordered list of candidate locators led by accessible names (which survive
Instagram's CSS-class churn), and is performed through `Human` so the pointer,
scrolling and typing move at a person's pace.

Everything after "Create" lives inside the upload modal and is looked up only
there. The feed behind the modal has its own "Share" (send) buttons and
carousel "Next" arrows, and they come earlier in the document, so an unscoped
lookup finds those first.

When a step can't be completed, the error names the step and a screenshot plus
DOM dump are saved, so repairing a selector is a quick edit.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Union

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from . import config
from .human import Human

INSTAGRAM = "https://www.instagram.com/"

# How long to wait for any single candidate locator before trying the next.
CANDIDATE_TIMEOUT_MS = 4_000
# Default for everything else. Typing is done key by key, so no single action
# needs to be long -- the original 4s default capped the whole caption.
DEFAULT_TIMEOUT_MS = 30_000
# How long the first Next may take to appear while the video is processed.
UPLOAD_TIMEOUT_MS = 120_000
# How long to wait for the dialog heading to change after clicking Next.
HEADING_CHANGE_TIMEOUT_MS = 20_000
# How long to wait for Instagram to confirm the post went live.
SHARE_TIMEOUT_MS = 300_000

DIALOG = '[role="dialog"]'

SUCCESS_TEXT = (
    "Your reel has been shared",
    "Your post has been shared",
    "Reel shared",
    "Post shared",
    "has been shared",
)
FAILURE_TEXT = (
    "couldn't be shared",
    "could not be shared",
    "Something went wrong",
)


class UploadError(Exception):
    """An upload step failed.

    retryable: False once Share has been clicked without confirmation, since
               the post may be live and retrying could publish it twice.
    abort_run: True when no further clip can succeed either (not logged in).
    """

    def __init__(
        self, step: str, message: str, retryable: bool = True, abort_run: bool = False
    ):
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.message = message
        self.retryable = retryable
        self.abort_run = abort_run


Root = Union[Page, Locator]
Candidate = Callable[[Root], Locator]

# step -> (where to look, candidates in priority order)
STEPS: dict[str, tuple[str, list[Candidate]]] = {
    "create": ("page", [
        lambda r: r.get_by_role("link", name="New post", exact=False),
        lambda r: r.get_by_role("button", name="New post", exact=False),
        lambda r: r.locator('svg[aria-label="New post"]'),
        lambda r: r.locator('[aria-label="New post"]'),
        lambda r: r.get_by_role("link", name="Create", exact=False),
        lambda r: r.get_by_role("button", name="Create", exact=False),
        lambda r: r.locator('[aria-label="Create"]'),
        lambda r: r.locator("span", has_text="Create"),
    ]),
    # Some layouts show a Post / Live / Ad menu after clicking Create.
    "create_submenu": ("page", [
        lambda r: r.get_by_role("link", name="Post", exact=True),
        lambda r: r.get_by_role("menuitem", name="Post", exact=True),
        lambda r: r.get_by_role("button", name="Post", exact=True),
        lambda r: r.locator('[aria-label="Post"]'),
    ]),
    "select_file": ("dialog", [
        lambda r: r.get_by_role("button", name="Select from computer"),
        lambda r: r.locator('button:has-text("Select from computer")'),
        lambda r: r.get_by_text("Select from computer", exact=False),
    ]),
    # "Video posts are now shared as reels" notice.
    "reels_notice_ok": ("dialog", [
        lambda r: r.get_by_role("button", name="OK", exact=True),
    ]),
    "crop_open": ("dialog", [
        lambda r: r.locator('svg[aria-label="Select crop"]'),
        lambda r: r.locator('[aria-label="Select crop"]'),
        lambda r: r.locator('[aria-label="Crop"]'),
    ]),
    "crop_original": ("dialog", [
        lambda r: r.get_by_role("button", name="Original", exact=True),
        lambda r: r.get_by_text("Original", exact=True),
        lambda r: r.get_by_text("9:16", exact=True),
    ]),
    "next": ("dialog", [
        lambda r: r.get_by_role("button", name="Next", exact=True),
        lambda r: r.locator('div[role="button"]:text-is("Next")'),
        lambda r: r.get_by_text("Next", exact=True),
    ]),
    "caption": ("dialog", [
        lambda r: r.get_by_role("textbox", name="Add a caption", exact=False),
        lambda r: r.get_by_role("textbox", name="Write a caption", exact=False),
        lambda r: r.locator('div[contenteditable="true"][aria-label*="aption"]'),
        lambda r: r.locator('[data-lexical-editor="true"]'),
    ]),
    "share": ("dialog", [
        lambda r: r.get_by_role("button", name="Share", exact=True),
        lambda r: r.locator('div[role="button"]:text-is("Share")'),
    ]),
    "close": ("page", [
        lambda r: r.locator('svg[aria-label="Close"]'),
        lambda r: r.locator('[aria-label="Close"]'),
    ]),
}


def _root(page: Page, scope: str) -> Root:
    # The upload modal is the most recently opened dialog.
    return page if scope == "page" else page.locator(DIALOG).last


def resolve(page: Page, step: str, timeout_ms: int = CANDIDATE_TIMEOUT_MS) -> Locator | None:
    """Try each candidate for `step` in order; return the first visible match."""
    scope, candidates = STEPS[step]
    root = _root(page, scope)
    for candidate in candidates:
        try:
            locator = candidate(root).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except Exception:
            continue
    return None


def require(page: Page, step: str, timeout_ms: int = CANDIDATE_TIMEOUT_MS) -> Locator:
    locator = resolve(page, step, timeout_ms)
    if locator is None:
        scope = STEPS[step][0]
        where = "the upload dialog" if scope == "dialog" else "the page"
        raise UploadError(
            step,
            f"no matching element found in {where}. Instagram's layout has probably "
            f"changed; check the screenshot in {config.FAILURES} and update "
            f"STEPS['{step}'] in upload.py.",
        )
    return locator


@contextmanager
def step(name: str) -> Iterator[None]:
    """Report any failure inside the block as a failure of this named step."""
    try:
        yield
    except UploadError:
        raise
    except PWTimeout as exc:
        raise UploadError(name, "timed out: " + str(exc).splitlines()[0]) from exc
    except Exception as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise UploadError(name, first) from exc


# ---------------------------------------------------------------------- session

def dismiss_dialogs(page: Page, human: Human | None = None) -> None:
    """Clear cookie banners and 'Save login info' / notification prompts.

    Declines optional cookies rather than accepting everything.
    """
    for label in (
        "Decline optional cookies",
        "Only allow essential cookies",
        "Not Now",
        "Not now",
    ):
        try:
            button = page.get_by_role("button", name=label, exact=False).first
            button.wait_for(state="visible", timeout=1_500)
            if human is not None:
                human.pause(0.6, 1.6)
                human.click(button)
            else:
                button.click()
            page.wait_for_timeout(400)
        except Exception:
            continue


def is_logged_in(page: Page) -> bool:
    """Logged-in sessions expose the Home nav; logged-out ones show a login form."""
    for candidate in (
        lambda p: p.locator('svg[aria-label="Home"]'),
        lambda p: p.locator('[aria-label="Home"]'),
        lambda p: p.get_by_role("link", name="Home", exact=False),
    ):
        try:
            candidate(page).first.wait_for(state="visible", timeout=5_000)
            return True
        except Exception:
            continue
    return False


def open_instagram(page: Page, human: Human | None = None, url: str = INSTAGRAM) -> None:
    page.goto(url, wait_until="domcontentloaded")
    if human is not None:
        human.pause(1.5, 3.5)  # let the page settle, as a person would
    dismiss_dialogs(page, human)


@dataclass
class BrowserSession:
    context: BrowserContext
    page: Page


@contextmanager
def browser(headless: bool = False, slow_mo: int = 0) -> Iterator[BrowserSession]:
    """A persistent Chrome profile, so the session survives between runs.

    This uses a dedicated profile directory (work/profile) rather than one of
    the user's own Chrome profiles under 'Chrome/User Data': driving those
    requires Chrome to be fully closed and risks corrupting real browsing data.
    """
    config.PROFILE.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        launch_kwargs = dict(
            user_data_dir=str(config.PROFILE),
            headless=headless,
            slow_mo=slow_mo,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            # Real Chrome presents a more ordinary fingerprint than bundled Chromium.
            ctx = pw.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        except Exception:
            ctx = pw.chromium.launch_persistent_context(**launch_kwargs)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        try:
            yield BrowserSession(context=ctx, page=page)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def save_failure_artifacts(page: Page, clip_id: int, step_name: str) -> Path:
    """Screenshot + DOM dump, so fixing a broken selector is a quick edit."""
    config.FAILURES.mkdir(parents=True, exist_ok=True)
    stem = config.FAILURES / f"clip{clip_id:04d}-{step_name}"
    png = stem.with_suffix(".png")
    try:
        page.screenshot(path=str(png), full_page=False)
    except Exception:
        pass
    try:
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return png


# ----------------------------------------------------------------------- upload

def _heading(page: Page) -> str:
    """The upload dialog's title: Create new post -> Crop -> Edit -> New reel."""
    try:
        heading = page.locator(DIALOG).last.get_by_role("heading").first
        if heading.count() == 0:
            return ""
        return heading.inner_text(timeout=2_000).strip()
    except Exception:
        return ""


def _advance(page: Page, human: Human, first_timeout_ms: int) -> None:
    """Click Next and confirm the dialog actually moved to the next screen.

    Next can be visible but inert while the video is still processing; a click
    then does nothing, and a blind second Next would land on the wrong screen.
    """
    before = _heading(page)
    timeout = first_timeout_ms
    for _ in range(3):
        human.click(require(page, "next", timeout))
        timeout = CANDIDATE_TIMEOUT_MS
        if not before:
            # No heading to watch; give the screen time to change and move on.
            human.pause(1.0, 2.0)
            return
        deadline = time.monotonic() + HEADING_CHANGE_TIMEOUT_MS / 1000
        now = before
        while time.monotonic() < deadline:
            now = _heading(page)
            if now and now != before:
                return
            page.wait_for_timeout(300)
        # Only click again if we are provably still on the same screen. If the
        # heading became unreadable, something changed, and a second Next
        # could skip a screen.
        if now != before:
            return
        human.pause(0.8, 1.8)
    raise UploadError("next", f"dialog stayed on {before!r} after clicking Next")


def _choose_file(page: Page, human: Human, video: Path) -> None:
    """Click "Select from computer" and answer the file dialog it opens.

    Playwright intercepts the native dialog, so nothing appears on screen, but
    the click itself happens exactly as a person's would.
    """
    button = resolve(page, "select_file", 10_000)
    if button is not None:
        try:
            with page.expect_file_chooser(timeout=10_000) as chooser:
                human.click(button)
            chooser.value.set_files(str(video.resolve()))
            return
        except PWTimeout:
            pass

    # Fallback for layouts where the button doesn't open a chooser directly.
    inputs = page.locator(f'{DIALOG} input[type="file"], input[type="file"]')
    try:
        inputs.first.wait_for(state="attached", timeout=10_000)
    except PWTimeout as exc:
        raise UploadError("select_file", "no file input appeared") from exc
    inputs.first.set_input_files(str(video.resolve()))


def _normalize(text: str) -> str:
    text = text.replace("​", "").replace("﻿", "")
    return " ".join(text.split())


def _write_caption(page: Page, human: Human, caption: str) -> None:
    """Type the caption, then read it back before anything can be shared.

    Typos are corrected as they happen, but the editor, hashtag suggestions or
    a stray key could still leave the text wrong, and a caption can't be fixed
    once the post is public.
    """
    box = require(page, "caption", 30_000)
    human.click(box)
    human.pause(0.5, 1.5)
    human.type_text(caption)

    got = box.inner_text()
    if _normalize(got) == _normalize(caption):
        return

    # One careful retry: clear the editor and type it again without slips.
    human.pause(1.0, 2.0)
    human.click(box)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    human.pause(0.5, 1.2)
    human.type_text(caption, allow_typos=False)

    got = box.inner_text()
    if _normalize(got) != _normalize(caption):
        raise UploadError(
            "caption",
            "the caption in the editor doesn't match what was intended, so the "
            f"post was not shared.\n  expected: {_normalize(caption)[:160]!r}"
            f"\n  found:    {_normalize(got)[:160]!r}",
        )


def wait_for_share(page: Page, timeout_ms: int | None = None) -> tuple[bool | None, str]:
    """Wait for Instagram to say how the share went.

    Returns (True, text) on confirmation, (False, text) on an explicit error,
    and (None, "") if neither appears in time. The Share button vanishing is
    not treated as success: it disappears as soon as sharing *starts*, and
    closing the browser at that point can abort the upload.
    """
    timeout_ms = SHARE_TIMEOUT_MS if timeout_ms is None else timeout_ms
    deadline = time.monotonic() + timeout_ms / 1000
    dialog = page.locator(DIALOG).last

    while time.monotonic() < deadline:
        for text in SUCCESS_TEXT:
            try:
                if page.get_by_text(text, exact=False).first.is_visible():
                    return True, text
            except Exception:
                pass
        for text in FAILURE_TEXT:
            try:
                if dialog.get_by_text(text, exact=False).first.is_visible():
                    return False, text
            except Exception:
                pass
        page.wait_for_timeout(1_000)

    return None, ""


def upload_clip(
    page: Page,
    video: Path,
    caption: str,
    human: Human,
    clip_id: int = 0,
    url: str = INSTAGRAM,
    on_share: Callable[[], None] | None = None,
) -> None:
    """Drive one reel upload at human pace. Raises UploadError naming the step.

    `on_share` runs immediately before Share is clicked, so the caller can
    record that the post may go live from this point on.
    """
    if not video.exists():
        raise UploadError("file", f"clip file is missing: {video}")

    with step("open"):
        open_instagram(page, human, url)
    if not is_logged_in(page):
        raise UploadError(
            "session",
            "not logged in. Run `reelpipe login` and sign in once by hand.",
            abort_run=True,
        )

    with step("browse"):
        human.browse_feed(human.rng.uniform(*human.cfg.browse_seconds))

    with step("create"):
        human.pause(0.5, 1.5)
        human.click(require(page, "create", 15_000))
        human.pause(0.4, 1.1)
        submenu = resolve(page, "create_submenu", 2_500)
        if submenu is not None:
            human.click(submenu)

    with step("select_file"):
        human.pause(0.8, 2.0)
        _choose_file(page, human, video)

    with step("reels_notice"):
        ok = resolve(page, "reels_notice_ok", 4_000)
        if ok is not None:
            human.pause(0.8, 2.0)  # reading the notice
            human.click(ok)

    # Clips are already exactly 1080x1920, so if the crop control isn't there
    # the framing is already right and this step is skipped.
    with step("crop"):
        human.pause(1.2, 3.0)  # looking at the preview
        crop = resolve(page, "crop_open", 8_000)
        if crop is not None:
            human.click(crop)
            human.pause(0.4, 1.0)
            original = resolve(page, "crop_original", 3_000)
            if original is not None:
                human.click(original)
                human.pause(0.4, 1.0)

    with step("next"):
        _advance(page, human, UPLOAD_TIMEOUT_MS)   # crop -> edit
        human.pause(1.5, 4.0)                      # cover and trim screen
        _advance(page, human, CANDIDATE_TIMEOUT_MS)  # edit -> caption

    with step("caption"):
        _write_caption(page, human, caption)
        human.pause(1.5, 4.0)  # re-reading before sharing

    with step("share"):
        share = require(page, "share", 15_000)
        if on_share is not None:
            on_share()
        human.click(share)

    # From here the post may already be live. A failure must not be retried
    # automatically, or the same clip could be published twice.
    try:
        ok, text = wait_for_share(page)
    except Exception as exc:
        raise UploadError(
            "share_confirm", f"lost track of the share: {exc}", retryable=False
        ) from exc
    if ok is False:
        raise UploadError("share_failed", f"Instagram reported: {text!r}")
    if ok is None:
        raise UploadError(
            "share_confirm",
            f"Share was clicked but no confirmation appeared within "
            f"{SHARE_TIMEOUT_MS // 1000}s. Check the profile before retrying.",
            retryable=False,
        )

    # Linger on the confirmation, then close it, as a person would.
    human.pause(*human.cfg.linger_seconds)
    close = resolve(page, "close", 2_000)
    if close is not None:
        try:
            human.click(close)
        except Exception:
            pass
