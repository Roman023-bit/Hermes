import pytest

from hermes_backup.config import ConfigError, load_settings


def test_missing_file_yields_documented_defaults(tmp_path):
    settings = load_settings(tmp_path / "absent.yaml")
    assert settings.retention_server == 7
    assert settings.retention_mac == 7
    assert settings.retention_mac_floor == 2
    assert settings.freshness_hours == 26
    assert settings.drill_staleness_hours == 48
    assert settings.network_lock_wait_seconds == 21600


def test_config_without_a_backup_section_yields_defaults(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("model: opus\n")
    assert load_settings(target).retention_mac == 7


def test_backup_section_overrides_defaults(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: 14\n  freshness_hours: 30\n")
    settings = load_settings(target)
    assert settings.retention_mac == 14
    assert settings.freshness_hours == 30
    assert settings.retention_server == 7


def test_unknown_key_is_rejected(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mars: 14\n")
    with pytest.raises(ConfigError, match="unknown"):
        load_settings(target)


def test_non_integer_value_is_rejected(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: plenty\n")
    with pytest.raises(ConfigError, match="retention_mac"):
        load_settings(target)


def test_booleans_are_not_integers(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: true\n")
    with pytest.raises(ConfigError, match="retention_mac"):
        load_settings(target)


def test_non_positive_value_is_rejected(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: 0\n")
    with pytest.raises(ConfigError, match="positive"):
        load_settings(target)


def test_floor_above_retention_is_rejected(tmp_path):
    """Keeping fewer copies than the floor demands would delete the floor."""
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: 2\n  retention_mac_floor: 5\n")
    with pytest.raises(ConfigError, match="floor"):
        load_settings(target)


def test_backup_section_must_be_a_mapping(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup: [1, 2]\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_settings(target)


def test_broken_yaml_is_rejected_not_ignored(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup: [unclosed\n")
    with pytest.raises(ConfigError):
        load_settings(target)
