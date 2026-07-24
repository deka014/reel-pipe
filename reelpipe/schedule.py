"""The posting schedule: slot times, batches, and changes to queue order.

A *slot* is one of the configured post times on some day. Each slot carries
`clips_per_slot` clips, uploaded back to back in one browser session. Clips in
the same slot are stamped one second apart, so `scheduled_for` alone always
gives a total order -- the queue order is simply "sort by scheduled_for".

Every reordering operation keeps the set of slot times and changes only which
clip sits in which one, so moving a clip never disturbs when things go out.
"""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import datetime, timedelta

from . import state
from .config import Pacing


class QueueError(Exception):
    """A queue operation was asked for something that no longer applies."""


# ------------------------------------------------------------------ slot times

def _slots_for_day(day: datetime, pacing: Pacing) -> list[datetime]:
    """The posting slots on a given date, earliest first."""
    times = pacing.post_times[: pacing.posts_per_day]
    return [
        day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        for t in times
    ]


def iter_slots(start_from: datetime, pacing: Pacing, count: int) -> list[datetime]:
    """Generate `count` future slots, walking forward day by day."""
    slots: list[datetime] = []
    if count <= 0:
        return slots
    day = start_from.replace(hour=0, minute=0, second=0, microsecond=0)
    # Ten years of days is far more headroom than any realistic backlog needs.
    for _ in range(3660):
        for slot in _slots_for_day(day, pacing):
            if slot > start_from:
                slots.append(slot)
                if len(slots) == count:
                    return slots
        day += timedelta(days=1)
    return slots


def plan_times(
    count: int,
    pacing: Pacing,
    start_from: datetime,
    now: datetime,
    rng: random.Random | None = None,
    previous: datetime | None = None,
) -> list[datetime]:
    """Times for `count` clips, `clips_per_slot` to a slot, after `start_from`.

    `previous` is the slot before the first new one, so the minimum gap holds
    across the join with what is already scheduled.
    """
    rng = rng or random
    if count <= 0:
        return []

    per = max(1, pacing.clips_per_slot)
    slots = iter_slots(start_from, pacing, math.ceil(count / per))
    min_gap = timedelta(minutes=pacing.min_gap_minutes)

    out: list[datetime] = []
    for slot in slots:
        when = slot + timedelta(
            minutes=rng.randint(-pacing.jitter_minutes, pacing.jitter_minutes)
        )
        # Jitter must never pull two slots closer than the configured gap.
        if previous is not None and when - previous < min_gap:
            when = previous + min_gap
        if when <= now:
            when = now + timedelta(minutes=1)
        when = when.replace(microsecond=0)
        for k in range(per):
            if len(out) == count:
                break
            out.append(when + timedelta(seconds=k))
        previous = when
    return out


def _extend(
    conn: sqlite3.Connection,
    count: int,
    pacing: Pacing,
    now: datetime,
    rng: random.Random | None,
) -> list[datetime]:
    """`count` new times continuing the existing schedule.

    A last slot that isn't full yet is topped up before a new one is opened.
    """
    if count <= 0:
        return []
    per = max(1, pacing.clips_per_slot)
    last = state.last_scheduled_time(conn)
    out: list[datetime] = []

    if last is not None and last > now and per > 1:
        in_last_slot = conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE status='queued' "
            "AND scheduled_for > ? AND scheduled_for <= ?",
            (
                (last - timedelta(seconds=per)).isoformat(timespec="seconds"),
                last.isoformat(timespec="seconds"),
            ),
        ).fetchone()["n"]
        room = max(0, per - int(in_last_slot))
        for k in range(1, min(room, count) + 1):
            out.append(last + timedelta(seconds=k))

    start_from = max(now, last) if last else now
    out += plan_times(count - len(out), pacing, start_from, now, rng, previous=last)
    return out


def assign_slots(
    conn: sqlite3.Connection,
    pacing: Pacing,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> int:
    """Give every rendered clip a scheduled_for time, in series order.

    New clips queue after whatever is already scheduled, so a second `prepare`
    run extends the queue rather than colliding with it.
    """
    now = now or datetime.now()
    pending = state.clips_needing_schedule(conn)
    if not pending:
        return 0
    times = _extend(conn, len(pending), pacing, now, rng)
    for clip, when in zip(pending, times):
        state.set_schedule(conn, int(clip["id"]), when)
    return min(len(pending), len(times))


# ------------------------------------------------------------- queue operations

def queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Queued clips in the order they will be posted."""
    return conn.execute(
        "SELECT * FROM clips WHERE status='queued' "
        "ORDER BY scheduled_for IS NULL, scheduled_for, video_id, part_no"
    ).fetchall()


def _times_of(rows: list[sqlite3.Row]) -> list[str]:
    return sorted(r["scheduled_for"] for r in rows if r["scheduled_for"])


def _apply(conn: sqlite3.Connection, ordered_ids: list[int], times: list[str]) -> None:
    for clip_id, when in zip(ordered_ids, times):
        conn.execute("UPDATE clips SET scheduled_for=? WHERE id=?", (when, clip_id))


def reorder(conn: sqlite3.Connection, ordered_ids: list[int]) -> None:
    """Put the queue in the given order, reusing the existing slot times."""
    rows = queue(conn)
    current = [int(r["id"]) for r in rows]
    if sorted(current) != sorted(ordered_ids) or len(set(ordered_ids)) != len(ordered_ids):
        raise QueueError("the queue changed in the meantime; refresh and try again")
    _apply(conn, ordered_ids, _times_of(rows))


def make_next(conn: sqlite3.Connection, clip_id: int) -> None:
    """Move one clip to the front of the queue."""
    current = [int(r["id"]) for r in queue(conn)]
    if clip_id not in current:
        raise QueueError(f"clip {clip_id} is not in the queue")
    current.remove(clip_id)
    reorder(conn, [clip_id] + current)


def skip(conn: sqlite3.Connection, clip_ids: list[int]) -> int:
    """Take clips out of the queue. Later clips move up to fill their slots."""
    wanted = sorted(set(clip_ids))
    if not wanted:
        return 0
    rows = queue(conn)
    times = _times_of(rows)

    # Failed clips can be skipped too; they just aren't holding a slot.
    cur = conn.execute(
        "UPDATE clips SET status='skipped', scheduled_for=NULL "
        f"WHERE status IN ('queued','failed') AND id IN ({','.join('?' * len(wanted))})",
        wanted,
    )
    if cur.rowcount == 0:
        return 0

    remaining = [int(r["id"]) for r in rows if int(r["id"]) not in set(wanted)]
    _apply(conn, remaining, times)
    return cur.rowcount


def unskip(
    conn: sqlite3.Connection,
    clip_ids: list[int],
    pacing: Pacing,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> int:
    """Put skipped clips back, each in front of its video's later parts."""
    now = now or datetime.now()
    if not clip_ids:
        return 0
    restore = conn.execute(
        "SELECT * FROM clips WHERE status='skipped' "
        f"AND id IN ({','.join('?' * len(clip_ids))}) ORDER BY video_id, part_no",
        clip_ids,
    ).fetchall()
    if not restore:
        return 0

    rows = queue(conn)
    info = {int(r["id"]): r for r in rows}
    order = [int(r["id"]) for r in rows]
    times = _times_of(rows)

    for clip in restore:
        position = None
        for i, queued_id in enumerate(order):
            other = info[queued_id]
            if other["video_id"] == clip["video_id"] and other["part_no"] > clip["part_no"]:
                position = i
                break
        if position is None:
            same_video = [
                i for i, queued_id in enumerate(order)
                if info[queued_id]["video_id"] == clip["video_id"]
            ]
            position = same_video[-1] + 1 if same_video else len(order)
        order.insert(position, int(clip["id"]))
        info[int(clip["id"])] = clip

    extra = _extend(conn, len(restore), pacing, now, rng)
    times += [t.isoformat(timespec="seconds") for t in extra]

    conn.execute(
        "UPDATE clips SET status='queued', attempts=0, last_error=NULL, "
        f"last_failed_step=NULL WHERE id IN ({','.join('?' * len(restore))})",
        [int(c["id"]) for c in restore],
    )
    _apply(conn, order, sorted(times))
    return len(restore)


def reschedule(
    conn: sqlite3.Connection,
    pacing: Pacing,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> int:
    """Lay the whole queue out again from now, keeping its order.

    For after the post times or batch size change, or when the queue has
    fallen behind and everything would otherwise be due at once.
    """
    now = now or datetime.now()
    rows = queue(conn)
    times = plan_times(len(rows), pacing, now, now, rng)
    _apply(
        conn,
        [int(r["id"]) for r in rows],
        [t.isoformat(timespec="seconds") for t in times],
    )
    return len(rows)


def set_time(conn: sqlite3.Connection, clip_id: int, when: datetime) -> None:
    """Pin one queued clip to a specific time; its place in the order follows."""
    cur = conn.execute(
        "UPDATE clips SET scheduled_for=? WHERE id=? AND status='queued'",
        (when.replace(microsecond=0).isoformat(timespec="seconds"), clip_id),
    )
    if cur.rowcount == 0:
        raise QueueError(f"clip {clip_id} is not in the queue")
