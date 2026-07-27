from __future__ import annotations

from pathlib import Path

import yaml


def write_alert_config(
    path: Path,
    *,
    state_root: Path,
    statuses: dict[str, tuple[Path, int, str | None]],
    fallback: bool = True,
) -> Path:
    components = {}
    for name, (status_file, age, unit) in statuses.items():
        components[name] = {
            "status_file": str(status_file),
            "max_age_seconds": age,
        }
        if unit:
            components[name]["unit"] = unit
    document = {
        "model": "unrelated-settings-are-preserved",
        "alerts": {
            "telegram": {
                "chat_ids": [350391119],
                "allow_primary_token_fallback": fallback,
            },
            "reminder_seconds": 3600,
            "profiles": {
                "test": {
                    "label": "TEST",
                    "state_root": str(state_root),
                    "components": components,
                }
            },
        },
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path
