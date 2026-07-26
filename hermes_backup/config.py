"""Paths and backup settings.

No new HERMES_* environment variables: AGENTS.md reserves .env for
credentials and puts every threshold and timeout in config.yaml. Paths
are constants here and are overridden by CLI arguments where a test or an
operator needs a different location.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

SERVER_DATA = Path("/srv/hermes/data")
SERVER_ESSENTIAL_ROOT = Path("/srv/hermes/backups/essential")
SERVER_FULL_ROOT = Path("/srv/hermes/backups")
SERVER_LOCK = Path("/run/lock/hermes-backup.lock")
SERVER_STATUS_DIR = Path("/var/lib/hermes-backup/status")
SERVER_CONFIG = SERVER_DATA / "config.yaml"

MAC_OFFSITE_ROOT = Path("~/.local/share/hermes/offsite-backups").expanduser()
MAC_STATUS_DIR = Path("~/.local/share/hermes/status").expanduser()
MAC_NETWORK_LOCK = Path(
    "~/Library/Application Support/offsite-sync/network.lock"
).expanduser()
MAC_CONFIG = Path("~/.hermes/config.yaml").expanduser()

REMOTE = "root@138.124.108.97"
SSH_KEY = Path("~/.ssh/aeza_hermes").expanduser()

DEFAULTS: dict[str, int] = {
    "retention_server": 7,
    "retention_mac": 7,
    "retention_mac_floor": 2,
    "freshness_hours": 26,
    "drill_staleness_hours": 48,
    "network_lock_wait_seconds": 6 * 3600,
}


class ConfigError(ValueError):
    """The backup section of config.yaml is malformed."""


@dataclass(frozen=True)
class BackupSettings:
    retention_server: int
    retention_mac: int
    retention_mac_floor: int
    freshness_hours: int
    drill_staleness_hours: int
    network_lock_wait_seconds: int


def load_settings(path: Path) -> BackupSettings:
    values = dict(DEFAULTS)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"{path}: {error}") from error

    section = raw.get("backup", {}) if isinstance(raw, dict) else None
    if section is None or not isinstance(section, dict):
        raise ConfigError(f"{path}: backup must be a mapping")

    for key, value in section.items():
        if key not in DEFAULTS:
            raise ConfigError(f"{path}: unknown backup key {key!r}")
        # bool is an int in Python, and `retention_mac: true` is a typo,
        # not a setting.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: {key} expects an integer")
        if value <= 0:
            raise ConfigError(f"{path}: {key} must be positive")
        values[key] = value

    if values["retention_mac_floor"] > values["retention_mac"]:
        raise ConfigError(
            f"{path}: retention_mac_floor exceeds retention_mac — the floor would be pruned"
        )
    return BackupSettings(**values)
