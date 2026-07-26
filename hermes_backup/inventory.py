"""What travels in the backup, what does not, and why.

Selection is "everything except the explicit exclusions", so an unknown
new file is backed up rather than silently lost. Classification is a
separate, purely descriptive step: unclassified files are counted so the
rules can be refreshed, never dropped.
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from hermes_backup.hashing import sha256_file

EXCLUDE_RULES: tuple[str, ...] = (
    "cache/*",
    # Dot-prefixed caches: `du /srv/hermes/data/*` never listed them, so the
    # first rule set missed 150 MB of npx and uv downloads in .npm and .cache.
    ".npm/*",
    ".cache/*",
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


@dataclass(frozen=True)
class ExclusionTotals:
    files: int
    specials: int
    escaping: int


def _kind(path: Path) -> str:
    """Classify by lstat alone: opening a FIFO would block forever."""
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def escapes_tree(link: Path, root: Path) -> bool:
    """True when a symlink points outside the tree.

    Judged lexically, never by resolving: the target may not exist, and
    resolving would follow it out of the tree to answer the question.
    """
    target = os.readlink(link)
    if os.path.isabs(target):
        return True
    resolved = os.path.normpath(os.path.join(link.parent, target))
    return os.path.relpath(resolved, root).startswith("..")


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


def _entries(root: Path):
    """Walk the tree without ever following a symlink.

    ``Path.rglob`` descends into symlinked directories, which can leave the
    tree, loop, or hash something that only looks local. Symlinks are
    recorded as themselves — dropping them would silently lose state, which
    is the one thing this backup must never do.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        linked_dirs = sorted(name for name in dirnames if (base / name).is_symlink())
        for name in linked_dirs:
            path = base / name
            yield path, path.relative_to(root).as_posix()
        dirnames[:] = sorted(set(dirnames) - set(linked_dirs))
        for name in sorted(filenames):
            path = base / name
            yield path, path.relative_to(root).as_posix()


def _describe(path: Path, rel: str, classification: str) -> dict:
    kind = _kind(path)
    if kind == "symlink":
        # readlink, never resolve: the target may be absent, external, or a
        # directory, and none of those may be followed or hashed.
        return {
            "path": rel,
            "type": "symlink",
            "target": os.readlink(path),
            "classification": classification,
        }
    if kind == "special":
        # No size, no SHA: reading a FIFO or a device would never return.
        return {"path": rel, "type": "special", "classification": classification}
    return {
        "path": rel,
        "type": "file",
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "classification": classification,
    }


def write_inventory(staging: Path, out: Path) -> InventoryTotals:
    files = total_bytes = unclassified = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _entries(staging):
            classification = classify(rel)
            record = _describe(path, rel, classification)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            files += 1
            total_bytes += record.get("size", 0)
            unclassified += classification == "unclassified"
    out.chmod(0o600)
    return InventoryTotals(
        files=files, total_bytes=total_bytes, unclassified=unclassified
    )


def write_exclusions(source: Path, out: Path) -> ExclusionTotals:
    """Record what never reached the archive, read from the live tree.

    Specials are recorded whether or not a rule matches them: rsync leaves
    them behind by design, and an unrecorded loss is exactly what this
    backup must not produce.
    """
    files = specials = escaping = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _entries(source):
            kind = _kind(path)
            rule = excluded_by(rel)
            if kind == "symlink" and escapes_tree(path, source):
                # rsync --safe-links leaves these behind, and the archive
                # validator would reject one anyway. Record it so a link
                # that mattered is visible rather than silently gone.
                record = {
                    "path": rel,
                    "rule": rule or "escaping-symlink",
                    "type": "symlink",
                    "target": os.readlink(path),
                    "classification": "excluded-escaping-link",
                }
                escaping += 1
            elif kind == "special":
                record = {
                    "path": rel,
                    "rule": rule or "special-object",
                    "type": "special",
                    "classification": "excluded-special",
                }
                specials += 1
            elif rule is None:
                continue
            elif kind == "symlink":
                record = {
                    "path": rel,
                    "rule": rule,
                    "type": "symlink",
                    "target": os.readlink(path),
                    "classification": "excluded-recoverable",
                }
                files += 1
            else:
                # No SHA here: exclusions describe what was left behind, and
                # hashing the recoverable bulk is the expensive half.
                record = {
                    "path": rel,
                    "rule": rule,
                    "type": "file",
                    "size": path.stat().st_size,
                    "classification": "excluded-recoverable",
                }
                files += 1
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    out.chmod(0o600)
    return ExclusionTotals(files=files, specials=specials, escaping=escaping)
