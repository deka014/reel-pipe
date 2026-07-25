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

