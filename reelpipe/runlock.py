"""One posting run at a time, across processes.

The control panel, its auto-poster and a Windows scheduled task can all start
`post`. Two at once would fight over the same browser profile. The lock is an
OS-level file lock, so it is released automatically if the holder crashes or
is killed -- there is no stale lock file to clean up.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

from . import config


class Busy(Exception):
    """Another posting run holds the lock."""


def _try_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


@contextmanager
def post_lock(wait: float = 0.0) -> Iterator[None]:
    """Hold the posting lock for the duration of the block.

    `wait` keeps retrying for that many seconds. When a posting process has
    just been killed, Windows releases its lock a moment after the process is
    gone rather than instantly, so cleanup after a stop has to allow for that.
    """
    config.WORK.mkdir(parents=True, exist_ok=True)
    handle = open(config.WORK / "post.lock", "a+b")
    locked = False
    try:
        deadline = time.monotonic() + wait
        locked = _try_lock(handle)
        while not locked and time.monotonic() < deadline:
            time.sleep(0.2)
            locked = _try_lock(handle)
        if not locked:
            raise Busy("another posting run is in progress")
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
