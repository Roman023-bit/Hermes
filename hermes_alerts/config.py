"""Strict alert configuration loaded from the existing Hermes config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class AlertConfigError(ValueError):
    """The alert configuration is absent or malformed."""


@dataclass(frozen=True)
class Component:
    name: str
    status_file: Path
    max_age_seconds: int
    unit: str | None = None


@dataclass(frozen=True)
class AlertSettings:
    profile: str
    label: str
    state_root: Path
    chat_ids: tuple[str, ...]
    allow_primary_token_fallback: bool
    reminder_seconds: int
    components: tuple[Component, ...]

    def component_for_unit(self, unit: str) -> str | None:
        for component in self.components:
            if component.unit == unit:
                return component.name
        return None


_ALERT_KEYS = frozenset({"telegram", "reminder_seconds", "profiles"})
_TELEGRAM_KEYS = frozenset({"chat_ids", "allow_primary_token_fallback"})
_PROFILE_KEYS = frozenset({"label", "state_root", "components"})
_COMPONENT_KEYS = frozenset({"status_file", "max_age_seconds", "unit"})


def _mapping(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise AlertConfigError(f"{where} must be a mapping")
    return value


def _unknown(mapping: dict, allowed: frozenset[str], where: str) -> None:
    extra = set(mapping) - allowed
    if extra:
        raise AlertConfigError(f"{where} has unknown keys: {sorted(extra)}")


def _positive_int(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AlertConfigError(f"{where} must be a positive integer")
    return value


def _path(value, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AlertConfigError(f"{where} must be a non-empty path")
    return Path(value).expanduser()


def load_settings(path: Path, profile: str) -> AlertSettings:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as error:
        raise AlertConfigError(f"{path}: missing") from error
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise AlertConfigError(f"{path}: {error}") from error
    root = _mapping(document, str(path))
    alerts = _mapping(root.get("alerts"), "alerts")
    _unknown(alerts, _ALERT_KEYS, "alerts")

    telegram = _mapping(alerts.get("telegram"), "alerts.telegram")
    _unknown(telegram, _TELEGRAM_KEYS, "alerts.telegram")
    raw_chat_ids = telegram.get("chat_ids")
    if not isinstance(raw_chat_ids, list) or not raw_chat_ids:
        raise AlertConfigError("alerts.telegram.chat_ids must be a non-empty list")
    chat_ids: list[str] = []
    for value in raw_chat_ids:
        text = str(value)
        if not text.lstrip("-").isdigit():
            raise AlertConfigError(f"invalid Telegram chat id: {value!r}")
        if text not in chat_ids:
            chat_ids.append(text)
    fallback = telegram.get("allow_primary_token_fallback", False)
    if not isinstance(fallback, bool):
        raise AlertConfigError(
            "alerts.telegram.allow_primary_token_fallback must be boolean"
        )

    reminder = _positive_int(
        alerts.get("reminder_seconds", 6 * 3600), "alerts.reminder_seconds"
    )
    profiles = _mapping(alerts.get("profiles"), "alerts.profiles")
    selected = _mapping(profiles.get(profile), f"alerts.profiles.{profile}")
    _unknown(selected, _PROFILE_KEYS, f"alerts.profiles.{profile}")
    label = selected.get("label")
    if not isinstance(label, str) or not label.strip():
        raise AlertConfigError(f"alerts.profiles.{profile}.label is required")
    state_root = _path(
        selected.get("state_root"), f"alerts.profiles.{profile}.state_root"
    )
    raw_components = _mapping(
        selected.get("components"), f"alerts.profiles.{profile}.components"
    )
    if not raw_components:
        raise AlertConfigError(f"alerts.profiles.{profile}.components is empty")
    components: list[Component] = []
    for name, raw in raw_components.items():
        if not isinstance(name, str) or not name.replace("_", "").isalnum():
            raise AlertConfigError(f"unsafe component name: {name!r}")
        component = _mapping(raw, f"component {name}")
        _unknown(component, _COMPONENT_KEYS, f"component {name}")
        unit = component.get("unit")
        if unit is not None and (
            not isinstance(unit, str)
            or not unit.endswith(".service")
            or "/" in unit
            or "\x00" in unit
        ):
            raise AlertConfigError(f"component {name}.unit is invalid")
        components.append(
            Component(
                name=name,
                status_file=_path(component.get("status_file"), f"{name}.status_file"),
                max_age_seconds=_positive_int(
                    component.get("max_age_seconds"), f"{name}.max_age_seconds"
                ),
                unit=unit,
            )
        )
    return AlertSettings(
        profile=profile,
        label=label.strip(),
        state_root=state_root,
        chat_ids=tuple(chat_ids),
        allow_primary_token_fallback=fallback,
        reminder_seconds=reminder,
        components=tuple(components),
    )
