from __future__ import annotations

import pytest

from hermes_alerts.config import AlertConfigError, load_settings

from conftest import write_alert_config


def test_valid_profile_is_loaded(tmp_path):
    status = tmp_path / "task.json"
    config = write_alert_config(
        tmp_path / "config.yaml",
        state_root=tmp_path / "state",
        statuses={"task": (status, 30, "task.service")},
    )
    settings = load_settings(config, "test")
    assert settings.label == "TEST"
    assert settings.chat_ids == ("350391119",)
    assert settings.components[0].status_file == status
    assert settings.component_for_unit("task.service") == "task"


@pytest.mark.parametrize(
    "fragment",
    [
        "alerts: []\n",
        "alerts:\n  unexpected: true\n",
        "alerts:\n  telegram:\n    chat_ids: []\n  profiles: {}\n",
    ],
)
def test_malformed_alert_root_is_rejected(tmp_path, fragment):
    target = tmp_path / "config.yaml"
    target.write_text(fragment, encoding="utf-8")
    with pytest.raises(AlertConfigError):
        load_settings(target, "test")


def test_unknown_component_key_is_rejected(tmp_path):
    config = write_alert_config(
        tmp_path / "config.yaml",
        state_root=tmp_path / "state",
        statuses={"task": (tmp_path / "task.json", 30, None)},
    )
    text = config.read_text(encoding="utf-8")
    config.write_text(
        text.replace("max_age_seconds: 30", "max_age_seconds: 30\n        typo: true")
    )
    with pytest.raises(AlertConfigError, match="unknown"):
        load_settings(config, "test")


def test_allowed_users_are_not_implicitly_alert_recipients(tmp_path):
    config = write_alert_config(
        tmp_path / "config.yaml",
        state_root=tmp_path / "state",
        statuses={"task": (tmp_path / "task.json", 30, None)},
    )
    text = config.read_text(encoding="utf-8")
    config.write_text("telegram:\n  allow_from: [1, 2]\n" + text)
    assert load_settings(config, "test").chat_ids == ("350391119",)
