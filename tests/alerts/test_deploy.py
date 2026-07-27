from __future__ import annotations

import plistlib
from pathlib import Path


ROOT = Path(__file__).parents[2]
SYSTEMD = ROOT / "deploy" / "beget" / "systemd"
MACOS = ROOT / "deploy" / "macos"


def test_backup_units_queue_failures_and_keep_exit_75_successful():
    for name in ("hermes-essential-backup.service", "hermes-full-backup.service"):
        text = (SYSTEMD / name).read_text(encoding="utf-8")
        assert "OnFailure=hermes-alert@%n.service" in text
        assert "SuccessExitStatus=75" in text


def test_alert_units_never_put_a_telegram_token_in_argv():
    for path in SYSTEMD.glob("hermes-alert*.service"):
        text = path.read_text(encoding="utf-8")
        assert "TELEGRAM_BOT_TOKEN" not in text
        assert "/srv/hermes/data/.env" in text or path.name != (
            "hermes-alert-delivery.service"
        )


def test_healthcheck_has_its_own_timer_and_on_failure():
    service = (SYSTEMD / "hermes-production-healthcheck.service").read_text()
    timer = (SYSTEMD / "hermes-production-healthcheck.timer").read_text()
    assert "OnFailure=hermes-alert@%n.service" in service
    assert "OnUnitActiveSec=10m" in timer


def test_three_alert_launchagents_have_expected_cadence():
    delivery = plistlib.loads((MACOS / "com.hermes.alert-delivery.plist").read_bytes())
    monitor = plistlib.loads((MACOS / "com.hermes.alert-monitor.plist").read_bytes())
    heartbeat = plistlib.loads(
        (MACOS / "com.hermes.alert-heartbeat.plist").read_bytes()
    )
    assert delivery["StartInterval"] == 120
    assert monitor["StartInterval"] == 300
    assert heartbeat["StartCalendarInterval"] == {
        "Weekday": 0,
        "Hour": 12,
        "Minute": 0,
    }
    assert delivery["RunAtLoad"] is True
    assert monitor["RunAtLoad"] is True
    assert "RunAtLoad" not in heartbeat


def test_alert_agents_use_installed_runtime_outside_documents():
    for path in MACOS.glob("com.hermes.alert-*.plist"):
        text = path.read_text(encoding="utf-8")
        assert "/.local/share/hermes/operations-runtime/run.sh" in text
        assert "/Documents/" not in text


def test_operations_runtime_has_no_gateway_dependency():
    text = (MACOS / "hermes_operations_runtime.sh").read_text(encoding="utf-8")
    installer = (MACOS / "install_hermes_operations.sh").read_text(encoding="utf-8")
    assert 'exec "$RUNTIME/venv/bin/python" -m "$@"' in text
    assert "hermes_alerts" in installer
    assert "hermes_backup" in installer
    assert "docker exec" not in text
    assert "hermes send" not in text
