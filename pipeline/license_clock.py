"""Persist the latest trusted wall-clock observation for commercial licenses.

This is a deterrent, not a replacement for online refresh: an attacker with
full local control can still modify local files.  It prevents simple clock
rollback from extending an offline license after the client has seen a newer
time.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class ClockCheck:
    ok: bool
    reason: str = ""


def last_seen_at(*, path: Path | None = None) -> int:
    target = path or config.LICENSE_CLOCK_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        value = int(payload.get("last_seen_at") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0
    return max(0, value)


def _save_last_seen(timestamp: int, *, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"last_seen_at": timestamp}), encoding="utf-8")
    os.replace(tmp, path)


def check_and_record(
    *,
    now: int | None = None,
    path: Path | None = None,
    tolerance_seconds: int | None = None,
) -> ClockCheck:
    """Reject a material rollback and otherwise retain the highest seen time."""
    current = int(now if now is not None else time.time())
    target = path or config.LICENSE_CLOCK_PATH
    tolerance = int(
        tolerance_seconds if tolerance_seconds is not None else config.LICENSE_CLOCK_ROLLBACK_TOLERANCE_SEC
    )
    previous = last_seen_at(path=target)
    if previous and current + tolerance < previous:
        return ClockCheck(False, "系统时间异常，请校准时间后联网刷新授权")
    if current > previous:
        try:
            _save_last_seen(current, path=target)
        except OSError:
            # Storage errors must not turn a valid paid license into a lockout.
            pass
    return ClockCheck(True)
