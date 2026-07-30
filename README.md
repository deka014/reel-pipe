# ReelPipe

Turn long YouTube videos into a steady stream of Instagram Reels, without
babysitting uploads.

ReelPipe downloads a video, cuts it into parts, formats each part for Reels
(1080×1920 with a "Part N" title burned in), writes the caption and hashtags,
and posts the parts on a schedule you set, from a control panel in your browser
or the command line.

> **Read [Responsible use](#responsible-use) before running this against an
> account you care about.**

---

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [The control panel](#the-control-panel)
- [Configuration](#configuration)
  - [Series](#series)
  - [Overlay text, emoji and other scripts](#overlay-text-emoji-and-other-scripts)
  - [Pacing and batches](#pacing-and-batches)
  - [Encoder](#encoder)
  - [Human pacing](#human-pacing)
  - [The video list: urls.txt](#the-video-list-urlstxt)
- [Scheduling and auto-post](#scheduling-and-auto-post)
- [How a post is made](#how-a-post-is-made)
- [Never posting twice](#never-posting-twice)
- [Command line](#command-line)
- [Clip statuses](#clip-statuses)
- [Troubleshooting](#troubleshooting)
- [Your data and privacy](#your-data-and-privacy)
- [Project layout](#project-layout)
- [Responsible use](#responsible-use)

---

## Features

**Processing**
- Downloads from YouTube with `yt-dlp`, skipping anything already downloaded.
- Cuts each video into parts of a length you choose. A short leftover at the
  end is merged into the last part rather than posted as a few-second stub.
- One ffmpeg pass per clip: seek, scale, letterbox to 1080×1920, burn on the
  overlay text and encode, all at once. Clips render in parallel.
- Overlay text with emoji and non-Latin scripts (Hindi and others), drawn
  properly in colour.
- Silent videos, odd aspect ratios and odd frame rates are handled.

**Posting**
- Uploads through a real Chrome window, driven at a person's pace: feed
  scrolling, curved mouse movement, uneven typing with occasional corrected
  typos.
- Reads the caption back before sharing and refuses to post a garbled one.
- Waits for Instagram's own "shared" confirmation before counting a post as done.
- Never re-posts a clip automatically once Share has been clicked.
- On failure, saves a screenshot and the page HTML, and names the step that broke.

**Scheduling**
- Daily post times, with a random shift so posts don't land on the dot.
- Batches: post 1, 2, 3 or more clips back to back at each post time.
- A queue that has fallen behind catches up gradually, never all at once.

**Control panel**
- See the whole queue with thumbnails, grouped by post time.
- Choose what goes next, drag to reorder, skip and restore clips or whole
  videos, post one or several now.
- Edit captions and times, preview clips, inspect failures with screenshots.
- Add videos, change the schedule, switch auto-post on and off, watch jobs live.

**Captions**
- Per-series caption templates and hashtag pools; each post gets a fresh random
  selection, never repeating one of the last few sets.

---

## How it works

```
urls.txt ──► download ──► split into parts ──► render 1080×1920 + overlay
                                                        │
                       ┌────────────────────────────────┘
                       ▼
                 queue (state.db) ──► post at scheduled times ──► Instagram
                       ▲                         ▲
                 control panel            auto-post / CLI
```

Everything is tracked in a small SQLite database (`state.db`), so every step
can be stopped and resumed, and nothing is downloaded, rendered or posted twice.

---

## Requirements

- **Windows 10 or 11.** ReelPipe was developed and tested on Windows 11. The core
  would likely run elsewhere, but the default font path, the launcher scripts and
  the background task are Windows-specific and untested on other systems.
- **Python 3.11 or newer** (3.13 tested).
- **ffmpeg** and **yt-dlp**, available on `PATH`.
- **Google Chrome** (recommended). Playwright's bundled Chromium is used as a
  fallback.
- An **Instagram account** you can log into in a browser.

---

## Installation

```powershell
git clone https://github.com/<you>/reelpipe.git
cd reelpipe

# Programs
winget install --id Gyan.FFmpeg --exact
winget install --id yt-dlp.yt-dlp --exact

# Python environment
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium

# Your own settings and video list, from the templates
copy config.example.toml config.toml
copy urls.example.txt urls.txt
```

Close and reopen the terminal after the `winget` installs so `ffmpeg` and
`yt-dlp` are on `PATH`. Then check everything:

```powershell
.\reelpipe.cmd doctor
```

`doctor` confirms the programs, the Python packages and your config, lists your
series, and measures encode speed on a short test clip.

---

## Quick start

1. **Describe your show** in `config.toml`: copy the example series and set its
   `id`, title, overlay text, caption and hashtags. See [Series](#series).

2. **Log into Instagram once:**

   ```powershell
   .\reelpipe.cmd login
   ```

   A Chrome window opens with a profile dedicated to ReelPipe. Sign in by hand.
   The session is kept in `work/profile/` and reused from then on. ReelPipe never
   sees or stores your password.

3. **Open the control panel:**

   ```powershell
   .\reelpipe.cmd ui
   ```

   or double-click **`ReelPipe Panel.cmd`**. It opens at `http://127.0.0.1:8765/`.
   Keep the terminal window open while you use it.

4. **Add videos** (*Add videos* tab): paste YouTube links, pick the series, then
   *Download & prepare*.

5. **Check the queue**, then post a clip with *Post now*, or turn on
   **Auto-post** to let the schedule run.

Everything in the panel can also be done from the [command line](#command-line).

---

## The control panel

Start it with `.\reelpipe.cmd ui` (options: `--port 8766`, `--no-open`). It only
answers on this computer.

### Queue

Every clip with its thumbnail, scheduled time and status, grouped by post time,
so a batch reads as one unit. The first clip is tagged **NEXT**. Tabs switch
between *Up next*, *Posted*, *Failed* and *Skipped*.

| Action | What it does |
|---|---|
| **Make next** | Moves a clip to the front of the queue. |
| **Drag a row** | Puts a clip anywhere in the order. |
| **Skip** | Takes a clip out of the queue; the clips behind it move up. |
| **Restore** (Skipped tab) | Puts a clip back, in front of its video's later parts. |
| **Post now** | Posts that one clip immediately. |
| **Post the next N** | Posts the next N clips now, one after another. |
| **Tick several → Post N now, together** | Posts exactly those clips, in queue order, in one session. |
| **Re-schedule from now** | Gives every queued clip a fresh time from now, keeping the order. |

Reordering swaps clips between the times already handed out, so moving clips
never changes *when* posts go out, only *which* clip goes at each time.

**Click a row** to open its details: play the clip, edit its caption, or pin it
to a specific time. For a failed clip you also see the error, the screenshot
taken when it failed, **Back to queue**, and **It did post — mark as posted** for
when you can see it on your profile.

### Videos

One card per video: how many parts are posted, queued, failed and skipped.
**Skip the rest** skips every remaining part; **Restore skipped** brings them
back; **Remove** deletes the video, its clips and its download from ReelPipe
(posts already on Instagram are not touched).

### Add videos

Paste one or more links, choose the series, *Add to list*. The links are written
to `urls.txt`. *Download & prepare* then downloads, splits, renders and queues
them, with the output shown live.

### Schedule

Edit post times, clips per post time, the pause between clips in a batch, the
random shift, the minimum gap, retries, and human pacing. *Save & re-schedule
queue* lays the queue out again with the new settings; *Save only* leaves the
times already given out unchanged. Settings are written into your `config.toml`
in place, keeping your comments.

Also here: **Keep posting with this panel closed** (a Windows scheduled task),
**Instagram login**, and **System check** (`doctor`).

### Activity

The live output of whatever is running, with a **Stop** button, earlier jobs,
and every failed clip with its reason.

---

## Configuration

All settings live in `config.toml`, which you create from
[`config.example.toml`](config.example.toml). Every setting is commented there.
`reelpipe doctor` validates the file and points at the exact problem if
something is wrong.

### Series

A *series* is a show or theme: its overlay text, caption, hashtags and part
length. Each video in `urls.txt` names the series it belongs to.

```toml
[[series]]
id            = "myshow"
title         = "My Show - Episode 1"
overlay       = "My Show 😂\nPart {part}"
caption       = "Watch Part {part} of {title}!\nCredit: {credit}"
credit        = "Original Studio"
part_seconds  = 60
hashtag_count = [5, 8]
hashtags      = ["#example", "#throwback", "#cartoon", "#reels", "#explorepage",
                 "#nostalgia", "#funny", "#trending", "#foryou"]
```

| Key | Default | Meaning |
|---|---|---|
| `id` | *required* | Name or number used in `urls.txt`. Unique, matched ignoring case, no spaces or `\|`. |
| `title` | *required* | Show title, available to templates as `{title}`. |
| `overlay` | `"Part {part}"` | Text burned onto the video. |
| `caption` | `"Part {part} of {title}"` | Instagram caption; hashtags are added after it. |
| `credit` | `""` | Available to templates as `{credit}`. |
| `part_seconds` | `40` | Length of each part (minimum 5). Keep it within Instagram's Reels length limit. |
| `min_tail_seconds` | `8` | A leftover shorter than this joins the previous part. |
| `hashtags` | `[]` | The pool to sample from. |
| `hashtag_count` | `[10, 15]` | How many to use per post: a number or `[min, max]`. Can't exceed the pool size; `0` for none. |
| `default` | `false` | Use this series for `urls.txt` lines with no id. At most one series. |
| `font` | `C:\Windows\Fonts\arialbd.ttf` | Font file for the overlay. |
| `font_size` | `64` | Pixels. |
| `font_color` | `"white"` | An ffmpeg colour: a name, `0xRRGGBB`, optionally `@alpha`. |
| `border_width` | `4` | Outline thickness in pixels; `0` for none. |
| `border_color` | `"black@0.85"` | Outline colour. |
| `text_y` | `400` | Distance from the top of the frame to the top of the text, in pixels (frame is 1920 tall). |

**Template placeholders** in `overlay` and `caption`: `{part}`, `{total}` (number
of parts), `{title}`, `{credit}`. `\n` is a line break.

### Overlay text, emoji and other scripts

Overlays may contain emoji and non-Latin text:

```toml
overlay = "Best moments 😂🔥\nPart {part}"
```

Plain English overlays are drawn by ffmpeg directly. ffmpeg's text drawing can
only use a single font, and no single font has both letters and emoji, so any
overlay containing emoji or non-Latin characters is drawn by a headless browser
first (which handles colour emoji and complex scripts) and stamped onto the
video as an image, in the same encode. Both kinds land at the same position with
the same line spacing; long lines wrap inside the frame.

If the browser can't draw an overlay, that clip fails and stays pending for the
next run, rather than being rendered with empty boxes where the emoji should be.

### Pacing and batches

```toml
[pacing]
posts_per_day     = 3
post_times        = ["09:30", "14:00", "19:30"]
jitter_minutes    = 20
min_gap_minutes   = 45
max_attempts      = 3
clips_per_slot    = 1
batch_gap_seconds = [45, 150]
```

| Key | Default | Meaning |
|---|---|---|
| `post_times` | `["09:30", "14:00", "19:30"]` | Daily post times, 24-hour `HH:MM`. |
| `posts_per_day` | number of times | How many of those times to use. Can't exceed the list. |
| `jitter_minutes` | `20` | Each time shifts randomly by up to ± this much. |
| `min_gap_minutes` | `45` | Minimum time between two posting sessions. Also the pace at which a late queue catches up. |
| `max_attempts` | `3` | Tries before a clip is parked as failed. |
| `clips_per_slot` | `1` | **Batch size**: clips posted back to back at each post time (1–10). |
| `batch_gap_seconds` | `[45, 150]` | Pause between clips in the same session, picked at random. |

With `clips_per_slot = 3` and three post times, nine clips go out a day, in
three sessions of three.

### Encoder

```toml
[encoder]
preset          = "veryfast"
crf             = 20
fps             = 30
audio_bitrate   = "128k"
workers         = 4
threads_per_job = 4
```

`preset` and `crf` are the usual x264 settings; `veryfast` at CRF 20 looks
right at Reels bitrates. `workers` clips render at once, each with
`threads_per_job` threads; keep their product near your CPU's logical core
count.

### Human pacing

```toml
[human]
enabled          = true
browse_seconds   = [10, 30]
typing_wpm       = [40, 70]
typo_rate        = 0.02
think_pause_rate = 0.06
linger_seconds   = [4, 12]
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` runs uploads at full speed, useful when fixing selectors. |
| `browse_seconds` | `[10, 30]` | How long to scroll the feed before each post. |
| `typing_wpm` | `[40, 70]` | Typing speed range; one speed is picked per caption. |
| `typo_rate` | `0.02` | Chance per letter of a neighbouring-key slip that gets corrected (0–0.3). |
| `think_pause_rate` | `0.06` | Chance per word of a longer pause (0–0.3). |
| `linger_seconds` | `[4, 12]` | How long to stay on the confirmation after posting. |

With these defaults a post takes about two to three minutes.

### The video list: urls.txt

One video per line, with the series id after it. Create it from
[`urls.example.txt`](urls.example.txt), or let the control panel create it when
you first add a link.

```
https://www.youtube.com/watch?v=AAAAAAAAAAA | myshow
https://youtu.be/BBBBBBBBBBB myshow
https://youtu.be/CCCCCCCCCCC
```

The separator can be `|` or a space. A line with no id uses the series marked
`default = true`. Lines starting with `#` are comments. An unknown id is
reported, with its line number, before anything is downloaded.

Changing a video's id takes effect on the next *prepare* as long as the video
hasn't been split into parts yet.

---

## Scheduling and auto-post

When clips are prepared, each one gets a time: the next free post time, filled
`clips_per_slot` at a time, after whatever is already queued.

**Auto-post** (the switch at the top right of the panel) posts clips as their
time comes, one post time's worth per run. If the queue has fallen behind, for
instance after the computer was off for a day, it does **not** post the backlog
all at once: it posts one batch, waits `min_gap_minutes`, then the next. The
panel shows a banner when clips are overdue, with *Re-schedule from now* to
spread them back out instead.

Auto-post runs while the panel is open. To keep posting with the panel closed,
use **Schedule → Keep posting with this panel closed → Install**. This adds a
Windows scheduled task named `ReelPipe` that checks every 10 minutes while you
are logged in (a visible browser needs your desktop session). It obeys the same
auto-post switch and logs to `work/logs/autopost.log`. Remove it from the same
place, or with `schtasks /Delete /TN "ReelPipe" /F`.

Only one posting run happens at a time, whichever way it was started: panel,
auto-post, scheduled task or command line.

---

## How a post is made

With human pacing on, each upload goes like this:

1. Open Instagram, let it load, and scroll the feed for a while: wheel flicks,
   pauses to look, the occasional scroll back up.
2. Click **Create → Post → Select from computer**. The file dialog is answered
   automatically, but the click is a real one.
3. Pause on each screen: the reels notice, the crop preview, the cover screen.
4. Type the caption with an uneven rhythm: slower after punctuation and line
   breaks, occasional pauses, and the odd mistyped letter, backspaced and fixed.
5. **Read the caption back.** If it doesn't match, clear it and type it again
   once without slips. If it still doesn't match, stop without sharing.
6. Click **Share**, wait for Instagram's *"Your reel has been shared"*, stay on it
   a few seconds, then close it.

The pointer travels along curved paths with easing, sometimes overshooting on
long moves, and clicks near the middle of each button rather than its exact
centre.

This changes how the interaction is *paced*. It does not hide that the browser
is automated, and it doesn't make automation undetectable.

---

## Never posting twice

The risky moment is after Share is clicked: the post may already be live even
if the confirmation never appears. So:

- Just before clicking Share, the clip is marked `sharing`.
- Confirmed by Instagram → `posted`.
- Instagram reports an error ("couldn't be shared") → safe to retry; it goes
  back in the queue.
- No confirmation, the browser closes, or the run is stopped after Share → the
  clip is parked as `failed` and **never retried automatically**. Check your
  profile: if it's there, use *It did post — mark as posted*; if not, *Back to
  queue*.

A missing login stops the run without using up any clip's retries. A run
stopped before Share simply returns its clip to the queue.

---

## Command line

Run commands as `.\reelpipe.cmd <command>` from the project folder.

| Command | What it does |
|---|---|
| `ui [--port N] [--no-open]` | Open the control panel. |
| `doctor [--no-benchmark]` | Check programs, packages and config; measure encode speed. |
| `login` | Open Chrome to sign into Instagram by hand (waits up to 5 minutes). |
| `prepare` | Download, split, render and queue everything in `urls.txt`. Safe to re-run. |
| `status` | Queue counts, next due time, upcoming posts, failures. |
| `show [videos\|clips\|hashtags]` | Print the database tables. Read-only. |
| `show --clip ID` | One clip in full, including its caption. |
| `show --status failed` | Only clips with that status. |
| `show --sql "SELECT ..."` | Any read-only query against the database. |
| `post --due` | Post one post time's worth of what's due (what auto-post runs). |
| `post --next N` | Post the next N queued clips now. |
| `post --ids 4,7,9` | Post exactly these clips now, in this order. |
| `post --dry-run` | Show what would be posted, without opening a browser. |
| `retry` | Put every failed clip back in the queue. |

---

## Clip statuses

| Status | Meaning |
|---|---|
| `pending` | Planned, not rendered yet (or its render failed; *prepare* retries it). |
| `rendered` | Rendered, waiting for a time. |
| `queued` | Has a time; waiting to be posted. |
| `posting` | Being uploaded right now. |
| `sharing` | Share has been clicked; waiting for confirmation. |
| `posted` | Live on Instagram. |
| `failed` | Gave up after retries, or may be live and needs checking. |
| `skipped` | Taken out of the queue by you; can be restored. |

---

## Troubleshooting

**`doctor` says ffmpeg or yt-dlp is missing.** Close and reopen the terminal
after installing with `winget` so `PATH` updates.

**"No config file at …config.toml".** Copy the template:
`copy config.example.toml config.toml`.

**A post fails with "not logged in".** Run `.\reelpipe.cmd login` (or *Schedule
→ Open login*) and sign in again. The clip keeps its retries.

**A post fails at a named step** (`create`, `next`, `caption`, `share`, …).
Instagram has probably changed its page. Open the screenshot (panel → the clip's
details, or `work/failures/`). Each step in `reelpipe/upload.py` has a list of
ways to find its button, tried in order (`STEPS`); add one that matches the new
page near the front of the list, then *Back to queue*. Setting
`[human] enabled = false` makes trying it again quicker.

**A clip is `failed` with step `share_confirm` or `interrupted`.** It may be
live. Check your profile, then *mark as posted* or *Back to queue*.

**A download failed.** Check the link works in a browser. Remove the video in
the *Videos* tab and add the link again.

**"Port 8765 is in use".** The panel is probably already open in another
window. Otherwise start it with `.\reelpipe.cmd ui --port 8766`.

**"Another posting run is in progress".** Wait for it to finish (or stop it in
the panel's *Activity* tab).

---

## Your data and privacy

These are personal and are listed in `.gitignore`, so they stay out of git:

| Path | Contains |
|---|---|
| `config.toml` | Your series, captions, credits and hashtags. |
| `urls.txt` | The videos you are processing. |
| `state.db` | Every video, clip, schedule and posting time. |
| `work/profile/` | **Your logged-in Instagram session.** Anyone with this folder is logged in as you. Never share it. |
| `work/downloads/`, `work/clips/`, `work/thumbs/` | Downloaded videos, rendered clips, thumbnails. |
| `work/failures/` | Screenshots and page HTML from failed uploads, which can show your account. |
| `work/logs/` | Background-task logs. |

Share `config.example.toml` and `urls.example.txt` instead.

The control panel listens on `127.0.0.1` only and refuses requests addressed to
any other host name, so other devices and other websites can't use it.

---

## Project layout

```
config.example.toml   settings template (copy to config.toml)
urls.example.txt      video list template (copy to urls.txt)
requirements.txt      Python packages
reelpipe.cmd          command-line launcher
ReelPipe Panel.cmd    double-click to open the control panel
autopost.pyw          what the optional Windows scheduled task runs
reelpipe/
  cli.py              commands
  config.py           loads and validates config.toml; edits it in place
  state.py            the SQLite job store
  fetch.py            yt-dlp downloads
  probe.py            ffprobe, and how a video is cut into parts
  render.py           single-pass ffmpeg rendering, in parallel
  textimage.py        draws emoji / non-Latin overlay text with a browser
  captions.py         overlay and caption text, hashtag selection
  schedule.py         post times, batches, reordering, skipping
  upload.py           the Instagram upload, step by step
  human.py            human-paced pointer, scrolling and typing
  runlock.py          one posting run at a time
  server.py           the control panel's local web server
  ui.html             the control panel page
```

To run against a separate data folder (for example a test setup), set the
`REELPIPE_HOME` environment variable to a folder with its own `config.toml`;
the database, `urls.txt` and `work/` are then kept there too.

---

## Responsible use

- **Instagram's Terms of Use don't allow automated access**, and accounts that
  automate posting can be restricted. ReelPipe paces its actions like a person
  but does not hide that it's automated. Use it on an account you're prepared to
  take that risk with.
- **Only post content you have the rights to.** Reposting other people's videos,
  shows or cartoons can get posts removed and the account struck for copyright,
  whatever tool posted them.
- Your Instagram session lives in `work/profile/`. Keep it private.
