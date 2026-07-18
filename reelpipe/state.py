"""SQLite job store. Uniqueness constraints make every command safe to re-run."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL,
    youtube_id  TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL DEFAULT '',
    duration_s  REAL,
    status      TEXT NOT NULL DEFAULT 'pending',
    source_path TEXT,
    -- The series chosen in urls.txt, stored so later stages don't re-derive it.
    series_id   TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id               INTEGER PRIMARY KEY,
    video_id         INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    part_no          INTEGER NOT NULL,
    total_parts      INTEGER NOT NULL,
    start_s          REAL NOT NULL,
    duration_s       REAL NOT NULL,
    path             TEXT,
    overlay_text     TEXT NOT NULL DEFAULT '',
    caption          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    scheduled_for    TEXT,
    posted_at        TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    last_failed_step TEXT,
    UNIQUE (video_id, part_no)
);

CREATE TABLE IF NOT EXISTS hashtag_history (
    id           INTEGER PRIMARY KEY,
    clip_id      INTEGER REFERENCES clips(id) ON DELETE CASCADE,
    tag_set_hash TEXT NOT NULL,
    used_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clips_status    ON clips(status);
CREATE INDEX IF NOT EXISTS idx_clips_scheduled ON clips(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_videos_status   ON videos(status);
"""

# Clip lifecycle: pending -> rendered -> queued -> posting -> sharing -> posted
#                                                          \-> failed
# 'sharing' is set just before Share is clicked. A clip found stuck there may
# already be public, so it is never re-queued automatically.
# 'skipped' clips were taken out of the queue by hand and can be put back.
CLIP_STATUSES = (
    "pending", "rendered", "queued", "posting", "sharing", "posted", "failed",
    "skipped",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "series_id" not in have:
        conn.execute(
            "ALTER TABLE videos ADD COLUMN series_id TEXT NOT NULL DEFAULT ''"
        )
        conn.commit()


def connect_readonly(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the database read-only, so inspecting it can never alter it."""
    db_path = db_path or config.DB_PATH
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- videos

def upsert_video(
    conn: sqlite3.Connection, url: str, youtube_id: str, series_id: str = ""
) -> int:
    """Register a video and its series. Returns the row id.

    Changing a URL's series id in urls.txt takes effect on a re-run as long as
    the video hasn't been split yet; after that its clips already exist.
    """
    conn.execute(
        "INSERT INTO videos (url, youtube_id, series_id, added_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(youtube_id) DO UPDATE SET series_id=excluded.series_id "
        "  WHERE videos.status IN ('pending','downloaded')",
        (url, youtube_id, series_id, now()),
    )
    row = conn.execute(
        "SELECT id FROM videos WHERE youtube_id = ?", (youtube_id,)
    ).fetchone()
    return int(row["id"])


def mark_downloaded(
    conn: sqlite3.Connection, video_id: int, title: str, duration: float, path: Path
) -> None:
    conn.execute(
        "UPDATE videos SET status='downloaded', title=?, duration_s=?, source_path=? "
        "WHERE id=?",
        (title, duration, str(path), video_id),
    )


def mark_video(conn: sqlite3.Connection, video_id: int, status: str) -> None:
    conn.execute("UPDATE videos SET status=? WHERE id=?", (status, video_id))


def get_video(conn: sqlite3.Connection, video_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()


def video_by_youtube_id(conn: sqlite3.Connection, yt_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM videos WHERE youtube_id=?", (yt_id,)).fetchone()


def videos_needing_download(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM videos WHERE status='pending' ORDER BY id"
    ).fetchall()


def videos_needing_split(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Downloaded videos that have no clip rows yet."""
    return conn.execute(
        "SELECT v.* FROM videos v "
        "WHERE v.status='downloaded' "
        "  AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.video_id = v.id) "
        "ORDER BY v.id"
    ).fetchall()


# ---------------------------------------------------------------------------- clips

def add_clip(
    conn: sqlite3.Connection,
    video_id: int,
    part_no: int,
    total_parts: int,
    start_s: float,
    duration_s: float,
    overlay_text: str,
    caption: str,
) -> int:
    """Insert a planned clip. Idempotent on (video_id, part_no)."""
    conn.execute(
        "INSERT INTO clips "
        "  (video_id, part_no, total_parts, start_s, duration_s, overlay_text, caption) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(video_id, part_no) DO NOTHING",
        (video_id, part_no, total_parts, start_s, duration_s, overlay_text, caption),
    )
    row = conn.execute(
        "SELECT id FROM clips WHERE video_id=? AND part_no=?", (video_id, part_no)
    ).fetchone()
    return int(row["id"])


def clips_needing_render(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, v.source_path, v.title AS video_title, v.youtube_id, "
        "       v.series_id "
        "FROM clips c JOIN videos v ON v.id = c.video_id "
        "WHERE c.status='pending' ORDER BY c.video_id, c.part_no"
    ).fetchall()


def mark_rendered(conn: sqlite3.Connection, clip_id: int, path: Path) -> None:
    conn.execute(
        "UPDATE clips SET status='rendered', path=?, last_error=NULL, "
        "last_failed_step=NULL WHERE id=?",
        (str(path), clip_id),
    )


def mark_clip_error(
    conn: sqlite3.Connection, clip_id: int, step: str, error: str
) -> None:
    """Record a render-stage failure without advancing the clip's status."""
    conn.execute(
        "UPDATE clips SET last_error=?, last_failed_step=? WHERE id=?",
        (error[:2000], step, clip_id),
    )


def clips_needing_schedule(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clips WHERE status='rendered' ORDER BY video_id, part_no"
    ).fetchall()


def set_schedule(conn: sqlite3.Connection, clip_id: int, when: datetime) -> None:
    conn.execute(
        "UPDATE clips SET status='queued', scheduled_for=? WHERE id=?",
        (when.isoformat(timespec="seconds"), clip_id),
    )


def last_scheduled_time(conn: sqlite3.Connection) -> datetime | None:
    """Latest slot already handed out, so new clips queue after it rather than clash."""
    row = conn.execute(
        "SELECT MAX(scheduled_for) AS m FROM clips "
        "WHERE scheduled_for IS NOT NULL AND status IN ('queued','posting','sharing')"
    ).fetchone()
    return datetime.fromisoformat(row["m"]) if row and row["m"] else None


def due_clips(
    conn: sqlite3.Connection, when: datetime, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = (
        "SELECT c.*, v.title AS video_title FROM clips c "
        "JOIN videos v ON v.id = c.video_id "
        "WHERE c.status='queued' AND c.scheduled_for <= ? "
        "ORDER BY c.scheduled_for, c.video_id, c.part_no"
    )
    params: list = [when.isoformat(timespec="seconds")]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def next_clips(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """The next N queued clips regardless of their slot -- for manual posting."""
    return conn.execute(
        "SELECT c.*, v.title AS video_title FROM clips c "
        "JOIN videos v ON v.id = c.video_id "
        "WHERE c.status='queued' "
        "ORDER BY c.scheduled_for IS NULL, c.scheduled_for, c.video_id, c.part_no "
        "LIMIT ?",
        (limit,),
    ).fetchall()


def claim_for_posting(conn: sqlite3.Connection, clip_id: int) -> bool:
    """Atomically move queued -> posting so a killed run cannot double-post.

    Returns False if another run already claimed this clip.
    """
    cur = conn.execute(
        "UPDATE clips SET status='posting', attempts=attempts+1 "
        "WHERE id=? AND status='queued'",
        (clip_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def release_claim(conn: sqlite3.Connection, clip_id: int) -> None:
    """Undo a claim without charging an attempt.

    For failures that aren't the clip's fault, such as the browser not being
    logged in, which would otherwise burn through a clip's retries.
    """
    conn.execute(
        "UPDATE clips SET status='queued', attempts=MAX(attempts-1, 0) "
        "WHERE id=? AND status='posting'",
        (clip_id,),
    )
    conn.commit()


def mark_posted(conn: sqlite3.Connection, clip_id: int) -> None:
    conn.execute(
        "UPDATE clips SET status='posted', posted_at=?, last_error=NULL, "
        "last_failed_step=NULL WHERE id=?",
        (now(), clip_id),
    )
    conn.commit()


def mark_sharing(conn: sqlite3.Connection, clip_id: int) -> None:
    """Record that Share is about to be clicked, before it is."""
    conn.execute(
        "UPDATE clips SET status='sharing' WHERE id=? AND status='posting'",
        (clip_id,),
    )
    conn.commit()


def unmark_sharing(conn: sqlite3.Connection, clip_id: int) -> None:
    """Instagram said outright that the share failed, so nothing went live:
    the clip can be treated like any other failed attempt again."""
    conn.execute(
        "UPDATE clips SET status='posting' WHERE id=? AND status='sharing'",
        (clip_id,),
    )
    conn.commit()


def mark_failed(
    conn: sqlite3.Connection, clip_id: int, step: str, error: str, max_attempts: int
) -> str:
    """Send a clip back to the queue for a retry, or park it as failed.

    A clip that got as far as Share is always parked, whatever the caller
    asked for: it may be live, and re-queueing it could post it twice.
    Returns the status it landed in.
    """
    row = conn.execute(
        "SELECT attempts, status FROM clips WHERE id=?", (clip_id,)
    ).fetchone()
    attempts = int(row["attempts"]) if row else max_attempts
    if row is not None and row["status"] == "sharing":
        max_attempts = 0
    status = "queued" if attempts < max_attempts else "failed"
    conn.execute(
        "UPDATE clips SET status=?, last_error=?, last_failed_step=? WHERE id=?",
        (status, error[:2000], step, clip_id),
    )
    conn.commit()
    return status


def reset_failed(conn: sqlite3.Connection) -> int:
    """Put failed clips back in the queue with a clean attempt count."""
    cur = conn.execute(
        "UPDATE clips SET status='queued', attempts=0, last_error=NULL, "
        "last_failed_step=NULL WHERE status='failed'"
    )
    return cur.rowcount


def reset_stuck_posting(conn: sqlite3.Connection) -> int:
    """Recover clips left in 'posting' by a killed run. Share was never
    clicked for these, so they are safe to try again."""
    cur = conn.execute("UPDATE clips SET status='queued' WHERE status='posting'")
    return cur.rowcount


def park_stuck_sharing(conn: sqlite3.Connection) -> int:
    """Clips a killed run left in 'sharing' may be live; park them for a check."""
    cur = conn.execute(
        "UPDATE clips SET status='failed', last_failed_step='interrupted', "
        "last_error='run stopped after Share was clicked; check the profile' "
        "WHERE status='sharing'"
    )
    return cur.rowcount


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM clips GROUP BY status"
    ).fetchall()
    out = {s: 0 for s in CLIP_STATUSES}
    for r in rows:
        out[r["status"]] = int(r["n"])
    return out


def next_due_time(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MIN(scheduled_for) AS m FROM clips WHERE status='queued'"
    ).fetchone()
    return row["m"] if row and row["m"] else None


def failed_clips(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, v.title AS video_title FROM clips c "
        "JOIN videos v ON v.id = c.video_id "
        "WHERE c.status='failed' ORDER BY c.video_id, c.part_no"
    ).fetchall()


# ------------------------------------------------------------------- hashtag history

def recent_tag_hashes(conn: sqlite3.Connection, limit: int = 3) -> set[str]:
    rows = conn.execute(
        "SELECT tag_set_hash FROM hashtag_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return {r["tag_set_hash"] for r in rows}


def record_tag_hash(conn: sqlite3.Connection, clip_id: int, tag_hash: str) -> None:
    conn.execute(
        "INSERT INTO hashtag_history (clip_id, tag_set_hash, used_at) VALUES (?, ?, ?)",
        (clip_id, tag_hash, now()),
    )


# ------------------------------------------------------------ control-panel actions

def _marks(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


def clips_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """Clips in exactly the order asked for; unknown ids are dropped."""
    if not ids:
        return []
    rows = conn.execute(
        "SELECT c.*, v.title AS video_title FROM clips c "
        f"JOIN videos v ON v.id = c.video_id WHERE c.id IN ({_marks(ids)})",
        ids,
    ).fetchall()
    by_id = {int(r["id"]): r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def requeue(conn: sqlite3.Connection, ids: list[int]) -> int:
    """Send failed clips back to the queue with a clean attempt count."""
    if not ids:
        return 0
    cur = conn.execute(
        "UPDATE clips SET status='queued', attempts=0, last_error=NULL, "
        "last_failed_step=NULL, scheduled_for=COALESCE(scheduled_for, ?) "
        f"WHERE status='failed' AND id IN ({_marks(ids)})",
        [now(), *ids],
    )
    return cur.rowcount


def mark_posted_manual(conn: sqlite3.Connection, ids: list[int]) -> int:
    """Record clips as posted by hand, e.g. after finding one live on the
    profile that was parked because its confirmation was never seen."""
    if not ids:
        return 0
    cur = conn.execute(
        "UPDATE clips SET status='posted', posted_at=?, last_error=NULL, "
        f"last_failed_step=NULL WHERE status IN ('failed','queued','skipped') "
        f"AND id IN ({_marks(ids)})",
        [now(), *ids],
    )
    return cur.rowcount


def set_caption(conn: sqlite3.Connection, clip_id: int, caption: str) -> bool:
    """Change a caption. Refused once the clip is being posted or is posted."""
    cur = conn.execute(
        "UPDATE clips SET caption=? WHERE id=? "
        "AND status NOT IN ('posting','sharing','posted')",
        (caption, clip_id),
    )
    return cur.rowcount == 1


def last_posted_time(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(posted_at) AS m FROM clips").fetchone()
    return datetime.fromisoformat(row["m"]) if row and row["m"] else None


def overdue_count(conn: sqlite3.Connection, when: datetime) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM clips WHERE status='queued' AND scheduled_for <= ?",
        (when.isoformat(timespec="seconds"),),
    ).fetchone()
    return int(row["n"])


def video_clip_ids(conn: sqlite3.Connection, video_id: int, statuses: tuple[str, ...]) -> list[int]:
    rows = conn.execute(
        f"SELECT id FROM clips WHERE video_id=? AND status IN ({_marks(list(statuses))}) "
        "ORDER BY part_no",
        [video_id, *statuses],
    ).fetchall()
    return [int(r["id"]) for r in rows]


def remove_video(conn: sqlite3.Connection, video_id: int) -> list[str]:
    """Delete a video and its clips from the database.

    Returns the rendered clip paths so the caller can delete the files.
    Refused while one of its clips is mid-upload.
    """
    busy = conn.execute(
        "SELECT COUNT(*) AS n FROM clips WHERE video_id=? "
        "AND status IN ('posting','sharing')",
        (video_id,),
    ).fetchone()["n"]
    if busy:
        raise RuntimeError("a clip from this video is being posted right now")
    paths = [
        r["path"] for r in conn.execute(
            "SELECT path FROM clips WHERE video_id=? AND path IS NOT NULL", (video_id,)
        )
    ]
    conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
    return paths


def all_clips(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, v.title AS video_title, v.series_id, v.youtube_id "
        "FROM clips c JOIN videos v ON v.id = c.video_id "
        "ORDER BY c.scheduled_for IS NULL, c.scheduled_for, c.video_id, c.part_no"
    ).fetchall()


def all_videos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM videos ORDER BY id").fetchall()


# ------------------------------------------------------------------------ settings

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
