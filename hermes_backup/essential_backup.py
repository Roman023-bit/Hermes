"""Build the off-site essential backup on Aeza.

Order matters: snapshots and staging first, then STATE and INVENTORY
computed from staging (never from the live tree, which keeps changing),
then the archive, then a self-check, and only then the atomic publish.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermes_backup import config
from hermes_backup.archive import create, validate
from hermes_backup.config import DEFAULTS, BackupSettings, load_settings
from hermes_backup.counters import (
    count_cron_jobs,
    count_plugins,
    count_sessions,
    count_skills,
)
from hermes_backup.hashing import atomic_write_text, sha256_file, write_sha256sums
from hermes_backup.inventory import write_exclusions, write_inventory
from hermes_backup.sqlite_snapshot import (
    foreign_key_check,
    integrity_check,
    page_count,
    user_version,
)
from hermes_backup.staging import stabilized_copy
from hermes_backup.state import format_state, parse_state
from hermes_backup.status import status_line, write_status

APP_ROOT = Path("/srv/hermes/app")
DATABASES = ("state.db", "kanban.db")
SIDECARS = ("-wal", "-shm")
MAX_STAGING_BYTES = 4 * 1024**3
FREE_SPACE_MARGIN = 512 * 1024**2


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def owner_of(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_uid, info.st_gid


def database_paths(data: Path) -> list[Path]:
    """Every artefact whose owner must agree, main databases required."""
    paths = [data]
    for name in DATABASES:
        main = data / name
        if not main.exists():
            raise RuntimeError(f"missing_database: {main}")
        paths.append(main)
        paths.extend(data / f"{name}{suffix}" for suffix in SIDECARS)
    return paths


def require_single_owner(paths: Sequence[Path]) -> tuple[int, int]:
    """One owner for the whole set, or we stop.

    A split owner means a previous run already wrote as the wrong user;
    chowning a live tree under a running Hermes would be worse than
    refusing to back up.
    """
    owners = {path: owner_of(path) for path in paths if path.exists()}
    distinct = set(owners.values())
    if len(distinct) != 1:
        raise RuntimeError(f"owner_mismatch: {owners}")
    return distinct.pop()


def snapshot_command(
    uid: int, gid: int, data: Path, dest: Path, names: Sequence[str]
) -> list[str]:
    # Drop privileges for this child only: the orchestrator still needs
    # docker inspect and root-only directories.
    return [
        "/usr/bin/setpriv",
        f"--reuid={uid}",
        f"--regid={gid}",
        "--clear-groups",
        "/usr/bin/python3",
        "-m",
        "hermes_backup.snapshot_cli",
        "--data",
        str(data),
        "--dest",
        str(dest),
        *names,
    ]


def _setpriv_runner(
    uid: int, gid: int, data: Path, dest: Path, names: Sequence[str]
) -> None:
    result = subprocess.run(
        snapshot_command(uid, gid, data, dest, names),
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(APP_ROOT), "PATH": "/usr/bin:/bin"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"snapshot_failed ({result.returncode}): {result.stderr.strip()}"
        )


def _git_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _image(field: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "-f", field, "hermes"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _tree_bytes(root: Path) -> int:
    """Size of regular files only, never following a symlink out of the tree."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
        for name in filenames:
            info = (base / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _grant_traversal(partial: Path, snapshots: Path, uid: int, gid: int) -> None:
    """Let the unprivileged snapshot child reach its own directory.

    The child runs as the Hermes uid, so a 0700 root:root parent would
    deny it before SQLite is even opened. 0710 with the child's group
    grants traversal and nothing else: the directory stays unreadable and
    unlistable, and the grant lasts only while the snapshot runs.
    """
    if os.geteuid() != 0:
        return
    os.chown(snapshots, uid, gid)
    snapshots.chmod(0o700)
    os.chown(partial, 0, gid)
    partial.chmod(0o710)


def _revoke_traversal(partial: Path) -> None:
    if os.geteuid() != 0:
        return
    os.chown(partial, 0, 0)
    partial.chmod(0o700)


def _validate_structured(staging: Path) -> None:
    """A file caught mid-write must never reach the archive.

    The lock stops other backups, not Hermes, so staging can hold a
    half-written config; parsing it here is the last gate before publish.
    """
    try:
        parsed = yaml.safe_load((staging / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RuntimeError(f"config_yaml_unparsable: {error}") from error
    if not parsed:
        raise RuntimeError("config_yaml_empty")


def run(
    data: Path,
    root: Path,
    *,
    rsync: str = "rsync",
    repo: Path | None = None,
    settings: BackupSettings | None = None,
    snapshot_runner=None,
) -> Path:
    settings = settings or BackupSettings(**DEFAULTS)
    runner = snapshot_runner or _setpriv_runner
    paths = database_paths(data)
    uid, gid = require_single_owner(paths)

    stamp = _stamp()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    partial = root / f".daily-{stamp}.partial"
    published = root / f"daily-{stamp}"
    if published.exists():
        # Two runs inside one second would otherwise overwrite each other.
        raise RuntimeError(f"already_published: {published}")

    source_bytes = _tree_bytes(data)
    free = shutil.disk_usage(root).free
    # Staging holds a copy and the archive is written beside it.
    needed = source_bytes * 2 + FREE_SPACE_MARGIN
    if free < needed:
        raise RuntimeError(f"insufficient_disk_space: free={free} needed={needed}")

    staging = partial / "staging"
    snapshots = partial / "snapshots"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True, mode=0o700)
    try:
        stabilized_copy(data, staging, rsync=rsync)
        staging_bytes = _tree_bytes(staging)
        if staging_bytes > MAX_STAGING_BYTES:
            raise RuntimeError(f"staging_too_large: {staging_bytes}")
        if shutil.disk_usage(root).free < staging_bytes + FREE_SPACE_MARGIN:
            raise RuntimeError("insufficient_disk_space_for_archive")
        _validate_structured(staging)

        snapshots.mkdir(mode=0o700)
        _grant_traversal(partial, snapshots, uid, gid)
        try:
            runner(uid, gid, data, snapshots, DATABASES)
        finally:
            # Give the private directory back even when the child failed:
            # a group-traversable partial must not outlive the snapshot.
            _revoke_traversal(partial)
        missing = [name for name in DATABASES if not (snapshots / name).exists()]
        if missing:
            raise RuntimeError(f"snapshot_missing: {missing}")
        # The child touched the live databases: prove it left them alone.
        require_single_owner(paths)

        databases = {}
        for name in DATABASES:
            source = snapshots / name
            integrity_check(source)
            foreign_key_check(source)
            databases[name] = {
                "sha256": sha256_file(source),
                "page_count": page_count(source),
                "user_version": user_version(source),
            }
            shutil.move(str(source), str(staging / name))
        shutil.rmtree(snapshots)

        totals = write_inventory(staging, partial / "INVENTORY.jsonl")
        exclusions = write_exclusions(data, partial / "EXCLUSIONS.jsonl")

        atomic_write_text(
            partial / "STATE",
            format_state({
                "BACKUP_FORMAT_VERSION": 1,
                "CREATED_AT": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "SOURCE_HOST": "aeza",
                "HERMES_GIT_SHA": _git_sha(repo or APP_ROOT),
                "HERMES_IMAGE_ID": _image("{{.Image}}"),
                "HERMES_IMAGE_REF": _image("{{index .Config.Image}}"),
                "STATE_DB_SHA256": databases["state.db"]["sha256"],
                "STATE_DB_PAGE_COUNT": databases["state.db"]["page_count"],
                "STATE_DB_USER_VERSION": databases["state.db"]["user_version"],
                "KANBAN_DB_SHA256": databases["kanban.db"]["sha256"],
                "KANBAN_DB_PAGE_COUNT": databases["kanban.db"]["page_count"],
                "KANBAN_DB_USER_VERSION": databases["kanban.db"]["user_version"],
                "EXPECTED_SESSIONS": count_sessions(
                    staging / "sessions" / "sessions.json"
                ),
                "EXPECTED_SKILLS": count_skills(staging / "skills"),
                "EXPECTED_PLUGINS": count_plugins(staging / "plugins"),
                "EXPECTED_CRON_JOBS": count_cron_jobs(staging / "cron" / "jobs.json"),
                "ESSENTIAL_FILE_COUNT": totals.files,
                "ESSENTIAL_TOTAL_BYTES": totals.total_bytes,
                "UNCLASSIFIED_FILE_COUNT": totals.unclassified,
                "EXCLUDED_SPECIAL_COUNT": exclusions.specials,
            }),
        )

        create(staging, partial / "essential.tar.gz")
        shutil.rmtree(staging)
        write_sha256sums(partial)
        _self_check(partial)
        partial.rename(published)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    _prune(root, settings.retention_server)
    return published


def _self_check(directory: Path) -> None:
    validate(directory / "essential.tar.gz")
    state = parse_state((directory / "STATE").read_text(encoding="utf-8"))
    exclusions = (directory / "EXCLUSIONS.jsonl").read_text(encoding="utf-8")
    recorded = sum(
        1
        for line in exclusions.splitlines()
        if json.loads(line)["classification"] == "excluded-special"
    )
    if recorded != state["EXCLUDED_SPECIAL_COUNT"]:
        raise RuntimeError(
            f"special_count_mismatch: STATE={state['EXCLUDED_SPECIAL_COUNT']} "
            f"file={recorded}"
        )
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if sha256_file(directory / name) != digest:
            raise RuntimeError(f"self_check_failed: {name}")


def _prune(root: Path, keep: int) -> None:
    if keep < 1:
        return
    daily = sorted(item for item in root.glob("daily-*") if item.is_dir())
    for stale in daily[: max(0, len(daily) - keep)]:
        shutil.rmtree(stale, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=config.SERVER_DATA)
    parser.add_argument("--root", type=Path, default=config.SERVER_ESSENTIAL_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.SERVER_STATUS_DIR)
    parser.add_argument("--config", type=Path, default=config.SERVER_CONFIG)
    args = parser.parse_args(argv)
    try:
        published = run(args.data, args.root, settings=load_settings(args.config))
    except BaseException as error:  # noqa: BLE001 — status must always be emitted
        write_status(args.status_dir, "essential_backup", "FAILED", reason=str(error))
        print(status_line("essential_backup", "FAILED", str(error)), file=sys.stderr)
        return 1
    state = parse_state((published / "STATE").read_text(encoding="utf-8"))
    write_status(args.status_dir, "essential_backup", "OK", backup_path=str(published))
    print(
        status_line(
            "essential_backup",
            "OK",
            f"path={published} files={state['ESSENTIAL_FILE_COUNT']} "
            f"unclassified={state['UNCLASSIFIED_FILE_COUNT']} "
            f"specials={state['EXCLUDED_SPECIAL_COUNT']}",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
