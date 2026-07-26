"""What travels in the backup, what does not, and why.

Selection is "everything except the explicit exclusions", so an unknown
new file is backed up rather than silently lost. Classification is a
separate, purely descriptive step: unclassified files are counted so the
rules can be refreshed, never dropped.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from hermes_backup.hashing import sha256_file

EXCLUDE_RULES: tuple[str, ...] = (
    "cache/*",
    "bin/*",
    "image_cache/*",
    "logs/*",
    "models_dev_cache.json",
    "cron/output/*",
    "sessions/request_dump_*.json",
    ".DS_Store",
    "*/.DS_Store",
    "state.db",
    "state.db-*",
    "kanban.db",
    "kanban.db-*",
    "**/__pycache__/*",
)
ESSENTIAL_RULES: tuple[str, ...] = (
    "state.db",
    "kanban.db",
    "config.yaml",
    "config.yaml.*",
    "auth.json",
    ".env",
    ".env.*",
    "sessions/sessions.json",
    "skills/*",
    "plugins/*",
    "workspace/*",
    "home/*",
    ".local/*",
    "cron/jobs.json",
    "cron/state/*",
)


@dataclass(frozen=True)
class InventoryTotals:
    files: int
    total_bytes: int
    unclassified: int


def _matches(rel: str, rules: tuple[str, ...]) -> str | None:
    for rule in rules:
        if fnmatch.fnmatch(rel, rule) or fnmatch.fnmatch(rel, f"{rule}/*"):
            return rule
    return None


def excluded_by(rel: str) -> str | None:
    """Return the exclusion rule that removes ``rel``, or None."""
    return _matches(rel, EXCLUDE_RULES)


def classify(rel: str) -> str:
    """Label a file that made it into staging."""
    return "essential" if _matches(rel, ESSENTIAL_RULES) else "unclassified"


def _relative_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path, path.relative_to(root).as_posix()


def write_inventory(staging: Path, out: Path) -> InventoryTotals:
    files = total_bytes = unclassified = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _relative_files(staging):
            size = path.stat().st_size
            classification = classify(rel)
            handle.write(
                json.dumps(
                    {
                        "path": rel,
                        "size": size,
                        "sha256": sha256_file(path),
                        "classification": classification,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            files += 1
            total_bytes += size
            unclassified += classification == "unclassified"
    out.chmod(0o600)
    return InventoryTotals(
        files=files, total_bytes=total_bytes, unclassified=unclassified
    )


def write_exclusions(source: Path, out: Path) -> int:
    """Record what the exclusion rules removed, read from the live tree."""
    count = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _relative_files(source):
            rule = excluded_by(rel)
            if rule is None:
                continue
            handle.write(
                json.dumps(
                    {
                        "path": rel,
                        "rule": rule,
                        "size": path.stat().st_size,
                        "classification": "excluded-recoverable",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            count += 1
    out.chmod(0o600)
    return count
