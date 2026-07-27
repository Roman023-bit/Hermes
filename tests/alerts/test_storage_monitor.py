from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from hermes_alerts.config import load_settings
from hermes_alerts.monitor import heartbeat, monitor, record_failure
from hermes_alerts.storage import enqueue, evaluate, read_event

from conftest import write_alert_config


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _status(path, outcome="OK", *, finished=NOW, last_ok_at=None, reason=""):
    record = {
        "name": path.stem,
        "outcome": outcome,
        "reason": reason,
        "backup_path": "",
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if last_ok_at is not None:
        record["last_ok_at"] = last_ok_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(record), encoding="utf-8")


def _settings(tmp_path, age=3600):
    status = tmp_path / "task.json"
    config = write_alert_config(
        tmp_path / "config.yaml",
        state_root=tmp_path / "alerts",
        statuses={"task": (status, age, "task.service")},
    )
    return load_settings(config, "test"), status


def _events(settings):
    return [
        read_event(path)
        for path in sorted((settings.state_root / "outbox").glob("*.json"))
    ]


def test_unknown_file_is_queued_privately_and_deduplicated(tmp_path):
    settings, _ = _settings(tmp_path)
    first = enqueue(settings, "task", "FAILED", "boom", dedupe_key="same")
    second = enqueue(settings, "task", "FAILED", "boom", dedupe_key="same")
    assert first == second
    assert first.stat().st_mode & 0o777 == 0o600
    assert first.parent.stat().st_mode & 0o777 == 0o700


def test_fresh_status_is_healthy(tmp_path):
    settings, status = _settings(tmp_path)
    _status(status)
    assert evaluate(settings.components[0], now=NOW) == (True, "ok age=0s")


def test_stale_and_failed_statuses_are_unhealthy(tmp_path):
    settings, status = _settings(tmp_path, age=60)
    _status(status, finished=NOW - timedelta(seconds=61))
    assert evaluate(settings.components[0], now=NOW)[0] is False
    _status(status, "FAILED", reason="database unavailable")
    assert evaluate(settings.components[0], now=NOW)[1].startswith("task_failed")


def test_skipped_uses_last_success_not_skip_timestamp(tmp_path):
    settings, status = _settings(tmp_path, age=3600)
    _status(
        status,
        "SKIPPED",
        finished=NOW,
        last_ok_at=NOW - timedelta(seconds=3601),
    )
    assert evaluate(settings.components[0], now=NOW)[0] is False


def test_failure_is_alerted_once_then_recovered(tmp_path):
    settings, status = _settings(tmp_path)
    _status(status, "FAILED", reason="boom")
    monitor(settings, now=NOW)
    monitor(settings, now=NOW + timedelta(minutes=1))
    assert [event["kind"] for event in _events(settings)] == ["FAILED"]

    _status(status, finished=NOW + timedelta(minutes=2))
    monitor(settings, now=NOW + timedelta(minutes=2))
    assert sorted(event["kind"] for event in _events(settings)) == [
        "FAILED",
        "RECOVERED",
    ]


def test_active_failure_gets_bounded_reminder(tmp_path):
    settings, status = _settings(tmp_path)
    _status(status, "FAILED", reason="boom")
    monitor(settings, now=NOW)
    monitor(settings, now=NOW + timedelta(minutes=59))
    monitor(settings, now=NOW + timedelta(hours=1))
    assert sorted(event["kind"] for event in _events(settings)) == [
        "FAILED",
        "REMINDER",
    ]


def test_systemd_failure_marks_component_active_without_duplicate(tmp_path):
    settings, status = _settings(tmp_path)
    _status(status, "FAILED", reason="boom")
    record_failure(settings, "task", "unit failed", dedupe_key="invocation", now=NOW)
    monitor(settings, now=NOW)
    assert [event["kind"] for event in _events(settings)] == ["FAILED"]


def test_heartbeat_reports_degraded_and_is_weekly_deduplicated(tmp_path):
    settings, _ = _settings(tmp_path)
    heartbeat(settings, now=NOW)
    heartbeat(settings, now=NOW + timedelta(days=1))
    events = _events(settings)
    assert len(events) == 1
    assert events[0]["kind"] == "HEARTBEAT"
    assert "DEGRADED" in events[0]["reason"]
