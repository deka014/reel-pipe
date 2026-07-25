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
