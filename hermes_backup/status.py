"""Machine-readable outcome of every run.

The summary command and, later, Telegram alerts read these files instead
of parsing free-form logs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup.hashing import atomic_write_text

OUTCOMES = frozenset({"OK", "FAILED", "SKIPPED"})
_SAFE_NAME = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_REQUIRED_FIELDS = ("name", "outcome", "reason", "backup_path", "finished_at")


class StatusError(ValueError):
    """A status name or outcome is not one this module will write."""


def status_line(name: str, outcome: str, reason: str = "") -> str:
    line = f"hermes_{name}_{outcome}"
    if not reason:
        return line
    # Keep it one line: a multi-line traceback in the reason would look
    # like several status lines to whoever greps the log.
    return f"{line} {' '.join(reason.split())}"


def write_status(
    directory: Path, name: str, outcome: str, reason: str = "", backup_path: str = ""
) -> Path:
    if not _SAFE_NAME.match(name):
        raise StatusError(f"unsafe status name: {name!r}")
    if outcome not in OUTCOMES:
        raise StatusError(f"unknown outcome: {outcome!r}")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / f"{name}.json"
    finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    last_ok_at = finished_at if outcome == "OK" else ""
    if not last_ok_at:
        previous = read_status(directory, name)
        if previous:
            if previous["outcome"] == "OK":
                last_ok_at = previous["finished_at"]
            else:
                last_ok_at = str(previous.get("last_ok_at", ""))
    atomic_write_text(
        target,
        json.dumps(
            {
                "name": name,
                "outcome": outcome,
                "reason": reason,
                "backup_path": backup_path,
                "finished_at": finished_at,
                "last_ok_at": last_ok_at,
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    return target


def read_status(directory: Path, name: str) -> dict | None:
    if not _SAFE_NAME.match(name):
        raise StatusError(f"unsafe status name: {name!r}")
    try:
        record = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or any(
        field not in record for field in _REQUIRED_FIELDS
    ):
        return None
    if record["outcome"] not in OUTCOMES:
        return None
    return record
