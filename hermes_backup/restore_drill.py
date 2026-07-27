"""Prove the pulled copy is restorable — without starting anything.

The drill never launches the container, the gateway or Telegram: the
archive holds live tokens, and a second Telegram poller would answer
Roman's messages twice.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermes_backup import config
from hermes_backup.archive import ArchiveError, extract
from hermes_backup.config import DEFAULTS, ConfigError, load_settings
from hermes_backup.counters import (
    CounterError,
    count_cron_jobs,
    count_plugins,
    count_sessions,
    count_skills,
)
from hermes_backup.hashing import sha256_file
from hermes_backup.inventory import write_inventory
from hermes_backup.offsite_pull import verify_backup
from hermes_backup.sqlite_snapshot import (
    SnapshotError,
    foreign_key_check,
    integrity_check,
    page_count,
    user_version,
)
from hermes_backup.status import status_line, write_status

SUPPORTED_FORMAT = 1
REQUIRED = ("auth.json", "config.yaml", "state.db", "kanban.db", "cron/jobs.json")
SECRETS = ("auth.json", "config.yaml", "sessions/sessions.json")
TOKEN_STORE = "mcp-tokens"
DATABASES = {
    "state.db": ("STATE_DB_SHA256", "STATE_DB_PAGE_COUNT", "STATE_DB_USER_VERSION"),
    "kanban.db": ("KANBAN_DB_SHA256", "KANBAN_DB_PAGE_COUNT", "KANBAN_DB_USER_VERSION"),
}


class DrillError(RuntimeError):
    """The backup failed a restore check."""


def _check_age(state: dict, staleness_hours: int) -> None:
    # CREATED_AT, not mtime: mtime says when the copy landed here, so an
    # old server archive pulled today would look brand new.
    try:
        created = datetime.strptime(
            str(state["CREATED_AT"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError as error:
        # An unparsable timestamp is a failed drill, not a traceback: the
        # status file must say what happened.
        raise DrillError(f"created_at_invalid {state['CREATED_AT']!r}") from error
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_hours > staleness_hours:
        raise DrillError(f"stale_backup age_hours={age_hours:.1f}")


def _require_regular_files(tree: Path) -> None:
    for name in REQUIRED:
        path = tree / name
        if not path.exists():
            raise DrillError(f"missing_required {name}")
        if path.is_symlink() or not path.is_file():
            raise DrillError(f"not_a_regular_file {name}")


def _check_databases(tree: Path, state: dict) -> None:
    for name, (sha_key, pages_key, version_key) in DATABASES.items():
        path = tree / name
        try:
            integrity_check(path)
            foreign_key_check(path)
        except SnapshotError as error:
            raise DrillError(f"integrity {name}: {error}") from error
        actual_sha = sha256_file(path)
        if actual_sha != state[sha_key]:
            raise DrillError(f"{sha_key} mismatch: {actual_sha} != {state[sha_key]}")
        if page_count(path) != state[pages_key]:
            raise DrillError(f"{pages_key} expected {state[pages_key]}")
        if user_version(path) != state[version_key]:
            raise DrillError(f"{version_key} expected {state[version_key]}")


def _check_counts(tree: Path, state: dict) -> dict:
    try:
        counts = {
            "sessions": count_sessions(tree / "sessions" / "sessions.json"),
            "skills": count_skills(tree / "skills"),
            "plugins": count_plugins(tree / "plugins"),
            "cron_jobs": count_cron_jobs(tree / "cron" / "jobs.json"),
        }
    except CounterError as error:
        raise DrillError(f"counter {error}") from error
    for key, state_key in (
        ("sessions", "EXPECTED_SESSIONS"),
        ("skills", "EXPECTED_SKILLS"),
        ("plugins", "EXPECTED_PLUGINS"),
        ("cron_jobs", "EXPECTED_CRON_JOBS"),
    ):
        if counts[key] != state[state_key]:
            raise DrillError(
                f"{state_key} expected {state[state_key]}, found {counts[key]}"
            )
    return counts


def _load_inventory(path: Path) -> list[dict]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as error:
        raise DrillError(f"inventory_unreadable {path.name}: {error}") from error
    return sorted(rows, key=lambda row: row.get("path", ""))


def _check_inventory(backup: Path, tree: Path, workdir: Path, state: dict) -> None:
    """Recount the tree and compare it with what the backup claims.

    Totals alone are not enough: a substituted INVENTORY.jsonl can keep the
    same file count and byte total while lying about every checksum, so the
    recorded rows are compared with the recomputed ones entry by entry.
    """
    recomputed_path = workdir / "inventory-recomputed.jsonl"
    totals = write_inventory(tree, recomputed_path)
    for actual, state_key in (
        (totals.files, "ESSENTIAL_FILE_COUNT"),
        (totals.total_bytes, "ESSENTIAL_TOTAL_BYTES"),
        (totals.unclassified, "UNCLASSIFIED_FILE_COUNT"),
    ):
        if actual != state[state_key]:
            raise DrillError(f"{state_key} expected {state[state_key]}, found {actual}")

    recorded = _load_inventory(backup / "INVENTORY.jsonl")
    recomputed = _load_inventory(recomputed_path)
    if recorded != recomputed:
        recorded_by_path = {row.get("path"): row for row in recorded}
        for row in recomputed:
            if recorded_by_path.get(row["path"]) != row:
                raise DrillError(
                    f"inventory_mismatch {row['path']}: "
                    f"recorded {recorded_by_path.get(row['path'])}, found {row}"
                )
        missing = {row.get("path") for row in recorded} - {
            row["path"] for row in recomputed
        }
        raise DrillError(f"inventory_mismatch: archive lacks {sorted(missing)}")


def _secret_paths(tree: Path):
    """Every file whose contents must stay owner-only.

    The historical configs matter as much as the live one: config.yaml.bak-*
    and config.yaml.pre-* carry the same provider keys and travel in the
    same archive.
    """
    candidates = [tree / name for name in SECRETS]
    candidates += sorted(tree.glob(".env*"))
    candidates += sorted(tree.glob("config.yaml.*"))
    for path in candidates:
        # lexists, not exists: a dangling symlink must be seen and rejected,
        # not silently skipped as "no such secret".
        if os.path.lexists(path):
            yield path


def _check_token_store(tree: Path) -> None:
    """OAuth tokens live here, so nothing about this directory is optional.

    The store itself may be absent — not every deployment has one — but if
    anything is there it must be a plain directory of plain files, all
    unreadable to anyone else, and every JSON in it must still parse.
    """
    store = tree / TOKEN_STORE
    # is_symlink before exists: a dangling symlink answers False to exists()
    # and would slip through as "no store at all".
    if store.is_symlink():
        raise DrillError(f"token_store_not_a_directory {TOKEN_STORE}")
    if not store.exists():
        return
    if not store.is_dir():
        raise DrillError(f"token_store_not_a_directory {TOKEN_STORE}")
    mode = stat.S_IMODE(store.stat().st_mode)
    if mode != 0o700:
        raise DrillError(f"token_store_mode {TOKEN_STORE} {mode:o}")

    # os.walk without following links: rglob would descend into a symlinked
    # directory before we ever got to reject it.
    for dirpath, dirnames, filenames in os.walk(store, followlinks=False):
        base = Path(dirpath)
        for name in sorted(dirnames):
            path = base / name
            rel = path.relative_to(tree)
            if path.is_symlink():
                raise DrillError(f"token_store_symlink {rel}")
            directory_mode = stat.S_IMODE(path.stat().st_mode)
            # A readable directory leaks the file names, which name the
            # providers a token exists for.
            if directory_mode != 0o700:
                raise DrillError(f"token_store_mode {rel} {directory_mode:o}")
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(tree)
            if path.is_symlink():
                raise DrillError(f"token_store_symlink {rel}")
            if not path.is_file():
                raise DrillError(f"token_store_special {rel}")
            file_mode = stat.S_IMODE(path.stat().st_mode)
            if file_mode & ~0o600:
                raise DrillError(f"permissions_too_wide {rel} {file_mode:o}")
            if path.suffix == ".json":
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise DrillError(f"token_unparsable {rel}: {error}") from error


def _require_private_modes(tree: Path) -> None:
    for path in _secret_paths(tree):
        rel = path.relative_to(tree)
        if path.is_symlink():
            raise DrillError(f"secret_not_a_regular_file {rel} (symlink)")
        if not path.is_file():
            raise DrillError(f"secret_not_a_regular_file {rel}")
        mode = stat.S_IMODE(path.stat().st_mode)
        # Anything outside owner read/write is wrong for a secret, execute
        # included: 0700 is not "no wider than 0600", it is a different mode
        # nothing in this tree should ever have.
        if mode & ~0o600:
            raise DrillError(f"permissions_too_wide {rel} {mode:o}")


def drill(
    backup: Path, *, staleness_hours: int = DEFAULTS["drill_staleness_hours"]
) -> dict:
    try:
        state = verify_backup(backup)
    except RuntimeError as error:
        raise DrillError(str(error)) from error
    if int(state["BACKUP_FORMAT_VERSION"]) != SUPPORTED_FORMAT:
        raise DrillError(f"format_version {state['BACKUP_FORMAT_VERSION']} unsupported")
    _check_age(state, staleness_hours)

    workdir = Path(tempfile.mkdtemp(prefix="hermes-drill-"))
    try:
        tree = workdir / "tree"
        try:
            extract(backup / "essential.tar.gz", tree)
        except ArchiveError as error:
            raise DrillError(f"archive_unsafe {error}") from error

        _require_regular_files(tree)
        _check_databases(tree, state)

        try:
            parsed_config = yaml.safe_load(
                (tree / "config.yaml").read_text(encoding="utf-8")
            )
        except yaml.YAMLError as error:
            raise DrillError(f"config_unparsable {error}") from error
        if not parsed_config:
            raise DrillError("config_empty")

        counts = _check_counts(tree, state)
        _check_inventory(backup, tree, workdir, state)
        _require_private_modes(tree)
        _check_token_store(tree)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    # The extracted tree holds live tokens: a drill that cannot remove it
    # has not finished, however healthy the backup turned out to be.
    shutil.rmtree(workdir, ignore_errors=True)
    if workdir.exists():
        raise DrillError(f"cleanup_failed: {workdir}")
    return {**counts, "unclassified": int(state["UNCLASSIFIED_FILE_COUNT"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=config.MAC_OFFSITE_ROOT)
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--status-dir", type=Path, default=config.MAC_STATUS_DIR)
    parser.add_argument("--config", type=Path, default=config.MAC_CONFIG)
    args = parser.parse_args(argv)

    backup = args.backup
    try:
        settings = load_settings(args.config)
        if backup is None:
            candidates = sorted(
                item for item in args.root.glob("daily-*") if item.is_dir()
            )
            if not candidates:
                raise DrillError("no_backup")
            backup = candidates[-1]
        summary = drill(backup, staleness_hours=settings.drill_staleness_hours)
    except (DrillError, ConfigError, OSError) as error:
        write_status(
            args.status_dir,
            "restore_drill",
            "FAILED",
            reason=str(error),
            backup_path=str(backup) if backup else "",
        )
        print(status_line("restore_drill", "FAILED", str(error)), file=sys.stderr)
        return 1
    write_status(args.status_dir, "restore_drill", "OK", backup_path=str(backup))
    print(
        status_line(
            "restore_drill",
            "OK",
            "sessions={sessions} skills={skills} plugins={plugins} "
            "cron_jobs={cron_jobs} unclassified={unclassified}".format(**summary),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
