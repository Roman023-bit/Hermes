"""Private state, event outbox and strict status evaluation."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup.hashing import atomic_write_text

from .config import AlertSettings, Component

_KINDS = frozenset({"FAILED", "REMINDER", "RECOVERED", "HEARTBEAT", "TEST"})
_STATUS_OUTCOMES = frozenset({"OK", "FAILED", "SKIPPED"})
_EVENT_REQUIRED = frozenset({
    "version",
    "event_id",
    "profile",
    "label",
    "component",
    "kind",
    "reason",
    "created_at",
    "pending_chat_ids",
    "attempts",
})
_SAFE_EVENT_ID = re.compile(r"\A[a-f0-9]{32}\Z")


class AlertStateError(ValueError):
    """A state, status or outbox record is malformed."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AlertStateError("timestamp is not a string")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise AlertStateError(f"invalid timestamp {value!r}") from error
    return parsed.replace(tzinfo=timezone.utc)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def state_lock(root: Path):
    private_dir(root)
    lock = root / "state.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def sanitize_reason(value: object, limit: int = 800) -> str:
    text = " ".join(str(value).split())
    text = text.replace("TELEGRAM_BOT_TOKEN", "[credential]")
    text = text.replace("TELEGRAM_ALERT_BOT_TOKEN", "[credential]")
    return text[:limit] or "unspecified"


def enqueue(
    settings: AlertSettings,
    component: str,
    kind: str,
    reason: str,
    *,
    dedupe_key: str,
) -> Path:
    if kind not in _KINDS:
        raise AlertStateError(f"unknown event kind: {kind}")
    event_id = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]
    outbox = private_dir(settings.state_root / "outbox")
    target = outbox / f"{event_id}.json"
    if target.exists():
        return target
    record = {
        "version": 1,
        "event_id": event_id,
        "profile": settings.profile,
        "label": settings.label,
        "component": component,
        "kind": kind,
        "reason": sanitize_reason(reason),
        "created_at": format_time(utc_now()),
        "pending_chat_ids": list(settings.chat_ids),
        "attempts": 0,
    }
    atomic_write_text(target, json.dumps(record, ensure_ascii=False) + "\n")
    return target


def read_event(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlertStateError(f"{path.name}: invalid JSON") from error
    if not isinstance(record, dict) or not _EVENT_REQUIRED <= set(record):
        raise AlertStateError(f"{path.name}: missing event fields")
    if record["version"] != 1 or record["kind"] not in _KINDS:
        raise AlertStateError(f"{path.name}: unsupported event")
    if not isinstance(record["event_id"], str) or not _SAFE_EVENT_ID.match(
        record["event_id"]
    ):
        raise AlertStateError(f"{path.name}: unsafe event id")
    if not isinstance(record["pending_chat_ids"], list):
        raise AlertStateError(f"{path.name}: invalid recipients")
    parse_time(record["created_at"])
    return record


def write_event(path: Path, record: dict) -> None:
    atomic_write_text(path, json.dumps(record, ensure_ascii=False) + "\n")


def load_monitor_state(root: Path) -> dict:
    path = root / "monitor-state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "components": {}}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlertStateError(f"{path}: unreadable monitor state") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("components"), dict)
    ):
        raise AlertStateError(f"{path}: invalid monitor state")
    return value


def write_monitor_state(root: Path, state: dict) -> None:
    private_dir(root)
    atomic_write_text(
        root / "monitor-state.json", json.dumps(state, ensure_ascii=False) + "\n"
    )


def _read_status(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AlertStateError("status_missing") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlertStateError("status_unreadable") from error
    if not isinstance(record, dict):
        raise AlertStateError("status_not_mapping")
    required = {"name", "outcome", "reason", "finished_at"}
    if not required <= set(record) or record["outcome"] not in _STATUS_OUTCOMES:
        raise AlertStateError("status_malformed")
    parse_time(record["finished_at"])
    return record


def evaluate(component: Component, *, now: datetime | None = None) -> tuple[bool, str]:
    now = now or utc_now()
    try:
        status = _read_status(component.status_file)
    except AlertStateError as error:
        return False, str(error)
    finished = parse_time(status["finished_at"])
    if finished.timestamp() > now.timestamp() + 300:
        return False, "status_from_future"
    outcome = status["outcome"]
    if outcome == "FAILED":
        return False, f"task_failed {sanitize_reason(status.get('reason'))}"
    reference = finished
    if outcome == "SKIPPED":
        last_ok = status.get("last_ok_at")
        if not last_ok:
            return False, "skipped_without_previous_success"
        try:
            reference = parse_time(last_ok)
        except AlertStateError:
            return False, "last_ok_at_invalid"
    age = max(0, int((now - reference).total_seconds()))
    if age > component.max_age_seconds:
        return False, f"stale age={age}s limit={component.max_age_seconds}s"
    return True, f"{outcome.lower()} age={age}s"
