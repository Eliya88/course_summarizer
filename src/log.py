"""Progress printing that stays readable while units run in parallel: each
line is written whole, prefixed with "[<unit number>] " inside a unit, which
is what the app's progress bar keys on."""
from __future__ import annotations

import sys
import threading

_lock = threading.Lock()
_local = threading.local()


def set_unit(n: int) -> None:
    _local.tag = f"[{n}] "


def log(msg: str) -> None:
    with _lock:
        sys.stdout.write(getattr(_local, "tag", "") + msg + "\n")
        sys.stdout.flush()
