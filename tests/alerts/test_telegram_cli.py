from __future__ import annotations

import json

from hermes_alerts import cli
from hermes_alerts.config import load_settings
from hermes_alerts.storage import enqueue, read_event
from hermes_alerts.telegram import DeliveryError, render_message

from conftest import write_alert_config


def _fixture(tmp_path):
    config = write_alert_config(
        tmp_path / "config.yaml",
        state_root=tmp_path / "alerts",
        statuses={"task": (tmp_path / "status.json", 30, None)},
    )
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    return config, env, load_settings(config, "test")


def test_message_contains_bounded_operational_fields(tmp_path):
    _, _, settings = _fixture(tmp_path)
    path = enqueue(
        settings,
        "backup",
        "FAILED",
        "line one\nline two",
        dedupe_key="message",
    )
    message = render_message(read_event(path))
    assert "[TEST] backup FAILED" in message
    assert "line one line two" in message
    assert len(message) <= 4096


def test_delivery_removes_event_only_after_all_recipients(tmp_path, monkeypatch):
    config, env, settings = _fixture(tmp_path)
    path = enqueue(settings, "task", "TEST", "hello", dedupe_key="deliver")
    sent = []
    monkeypatch.setattr(
        cli,
        "send_message",
        lambda token, chat, text, **kwargs: (
            sent.append((token, chat, kwargs["silent"])) or "1"
        ),
    )
    code = cli.main([
        "--config",
        str(config),
        "--profile",
        "test",
        "deliver",
        "--env-file",
        str(env),
    ])
    assert code == 0
    assert sent == [("123:abc", "350391119", True)]
    assert not path.exists()


def test_failed_delivery_stays_in_outbox_with_attempt_count(tmp_path, monkeypatch):
    config, env, settings = _fixture(tmp_path)
    path = enqueue(settings, "task", "FAILED", "boom", dedupe_key="retry")

    def fail(*_args, **_kwargs):
        raise DeliveryError("offline")

    monkeypatch.setattr(cli, "send_message", fail)
    assert (
        cli.main([
            "--config",
            str(config),
            "--profile",
            "test",
            "deliver",
            "--env-file",
            str(env),
        ])
        == 1
    )
    record = json.loads(path.read_text())
    assert record["attempts"] == 1
    assert record["pending_chat_ids"] == ["350391119"]


def test_invalid_outbox_record_is_quarantined(tmp_path, monkeypatch):
    config, env, settings = _fixture(tmp_path)
    outbox = settings.state_root / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "broken.json").write_text("{")
    monkeypatch.setattr(cli, "send_message", lambda *_: "1")
    assert (
        cli.main([
            "--config",
            str(config),
            "--profile",
            "test",
            "deliver",
            "--env-file",
            str(env),
        ])
        == 0
    )
    assert (settings.state_root / "bad" / "broken.json").exists()


def test_systemd_unit_validator_accepts_units_not_paths():
    assert cli._SAFE_UNIT.match("knowledge-factory-backup.service")
    assert cli._SAFE_UNIT.match("hermes-alert-drill.service")
    assert not cli._SAFE_UNIT.match("../../escape.service")
    assert not cli._SAFE_UNIT.match("timer.timer")
