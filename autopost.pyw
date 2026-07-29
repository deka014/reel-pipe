"""Run by the optional Windows scheduled task, with no console window.

Posts whatever is due, exactly like `reelpipe post --due`, but only while
auto-post is switched on in the control panel. Output goes to
work/logs/autopost.log.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from reelpipe import config, state  # noqa: E402

config.ensure_dirs()

with state.session() as conn:
    enabled = state.get_setting(conn, "auto_post", "0") == "1"
    due = bool(state.due_clips(conn, datetime.now(), limit=1)) if enabled else False

# Nothing due is the normal case every ten minutes; stay out of the log for it.
if enabled and due:
    log = open(config.LOGS / "autopost.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    from reelpipe import cli  # noqa: E402

    code = cli.main(["post", "--due"])
    log.close()
    sys.exit(code)
