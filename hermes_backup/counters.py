"""Counting rules shared by the backup and the drill.

Verified against the live tree on 2026-07-26: skills nest as
category/skill (SKILL.md appears 78 times below 30 top-level dirs),
plugin.yaml sits at depth two for image_gen/replicate, and sessions/
holds one sessions.json beside debug request dumps.
"""

from __future__ import annotations

import json
from pathlib import Path


class CounterError(ValueError):
    """A counted artefact is missing or has an unexpected shape."""


def _count_marker(root: Path, marker: str) -> int:
    if not root.is_dir():
        raise CounterError(f"not a directory: {root}")
    return sum(1 for _ in root.rglob(marker) if _.is_file())


def count_skills(skills_dir: Path) -> int:
    return _count_marker(skills_dir, "SKILL.md")


def count_plugins(plugins_dir: Path) -> int:
    return _count_marker(plugins_dir, "plugin.yaml")


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CounterError(f"{path}: {error}") from error


def count_sessions(sessions_json: Path) -> int:
    payload = _load_json(sessions_json)
    if not isinstance(payload, dict):
        raise CounterError(f"{sessions_json}: expected an object of sessions")
    return len(payload)


def count_cron_jobs(jobs_json: Path) -> int:
    payload = _load_json(jobs_json)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise CounterError(f'{jobs_json}: expected {{"jobs": [...]}}')
    return len(payload["jobs"])
