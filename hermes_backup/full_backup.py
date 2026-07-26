"""The full local archive on Aeza: everything, with consistent databases.

This tier is the safety net for files the essential classification never
knew about, so it keeps caches and junk. What it must not keep is a live
SQLite file: the databases are replaced by snapshots taken by an
unprivileged child, exactly as the essential backup does.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup import config
from hermes_backup.config import DEFAULTS, BackupSettings, load_settings
from hermes_backup.essential_backup import (
    DATABASES,
    database_paths,
    require_single_owner,
    setpriv_runner,
)
from hermes_backup.sqlite_snapshot import foreign_key_check, integrity_check
from hermes_backup.status import status_line, write_status


def run(
    data: Path,
    backup_dir: Path,
    *,
    settings: BackupSettings | None = None,
    snapshot_runner=None,
) -> Path:
    settings = settings or BackupSettings(**DEFAULTS)
    runner = snapshot_runner or setpriv_runner
    paths = database_paths(data)
    uid, gid = require_single_owner(paths)

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    # Microseconds, not seconds: two runs in the same second would other-
    # wise write the same name and the second would replace the first.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    archive = backup_dir / f"hermes-{stamp}.tar.gz"
    if archive.exists():
        raise RuntimeError(f"already_published: {archive}")
    # with_suffix would turn hermes-x.tar.gz into hermes-x.tar.tar.gz.partial.
    partial = Path(f"{archive}.partial")

    # PrivateTmp keeps this out of the host's /tmp; the child needs to own
    # it, so it cannot live inside the root-only backup directory.
    snapshots = Path(tempfile.mkdtemp(prefix="hermes-full-snapshots-"))
    snapshots_removed = False
    try:
        if os.geteuid() == 0:
            os.chown(snapshots, uid, gid)
        snapshots.chmod(0o700)
        runner(uid, gid, data, snapshots, DATABASES)
        missing = [name for name in DATABASES if not (snapshots / name).exists()]
        if missing:
            raise RuntimeError(f"snapshot_missing: {missing}")
        for name in DATABASES:
            integrity_check(snapshots / name)
            foreign_key_check(snapshots / name)
        # The child touched the live databases: prove it left them alone.
        require_single_owner(paths)

        command = ["tar", "-czf", str(partial)]
        live = _live_entries(data)
        if live:
            command += ["-C", str(data), *live]
        command += ["-C", str(snapshots), *DATABASES]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            # No-op under GNU tar; on a developer Mac it stops bsdtar from
            # storing an AppleDouble "._state.db" beside every member,
            # which reads back as a corrupt database.
            env={**os.environ, "COPYFILE_DISABLE": "1"},
        )
        # GNU tar exits 1 when a file changed while being read, which is
        # expected against a live Hermes; only >=2 is fatal.
        if result.returncode >= 2:
            raise RuntimeError(
                f"tar_failed ({result.returncode}): {result.stderr.strip()}"
            )

        verify = subprocess.run(
            ["tar", "-tzf", str(partial)], capture_output=True, text=True, check=False
        )
        if verify.returncode != 0:
            raise RuntimeError("archive_unreadable")
        _require_snapshot_layout(verify.stdout)

        # Remove the snapshots before publishing, and fail if that does not
        # work: an archive must never be announced as done while a readable
        # copy of both databases is still lying around.
        shutil.rmtree(snapshots)
        if snapshots.exists():
            raise RuntimeError(f"snapshot_cleanup_failed: {snapshots}")
        snapshots_removed = True

        partial.replace(archive)
        archive.chmod(0o600)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if not snapshots_removed:
            shutil.rmtree(snapshots, ignore_errors=True)

    _prune(backup_dir, settings.retention_server)
    return archive


def _live_entries(data: Path) -> list[str]:
    """Top-level names to archive from the live tree, databases removed.

    Naming the entries instead of passing "." with --exclude is what keeps
    the snapshots in the archive: libarchive strips a leading "./" from
    both the pattern and the member name before matching, so an
    --exclude=./state.db* meant for the live tree silently drops the
    snapshot that follows it under a second -C as well.
    """
    return sorted(
        f"./{entry.name}"
        for entry in data.iterdir()
        if not any(entry.name.startswith(database) for database in DATABASES)
    )


def _require_snapshot_layout(listing: str) -> None:
    """The archive must carry one snapshot per database and no sidecars."""
    names = [line.strip().lstrip("./") for line in listing.splitlines() if line.strip()]
    for name in DATABASES:
        if names.count(name) != 1:
            raise RuntimeError(f"archive_layout: {names.count(name)} copies of {name}")
    strays = [
        name
        for name in names
        if any(name.startswith(f"{database}-") for database in DATABASES)
    ]
    if strays:
        raise RuntimeError(f"archive_layout: live sidecars in archive: {strays}")


def _prune(backup_dir: Path, keep: int) -> None:
    # Never prune to nothing: if retention is misconfigured, keep history.
    if keep < 1:
        return
    archives = sorted(backup_dir.glob("hermes-*.tar.gz"))
    for stale in archives[: max(0, len(archives) - keep)]:
        stale.unlink(missing_ok=True)


def main(argv: list[str] | None = None, *, snapshot_runner=None) -> int:
    """Entry point.

    ``snapshot_runner`` is a keyword argument rather than a CLI flag on
    purpose: a command-line switch would be a production-reachable way to
    bypass setpriv, and only pytest has any business passing one.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=config.SERVER_DATA)
    parser.add_argument("--backup-dir", type=Path, default=config.SERVER_FULL_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.SERVER_STATUS_DIR)
    parser.add_argument("--config", type=Path, default=config.SERVER_CONFIG)
    parser.add_argument(
        "--record-skip",
        action="store_true",
        help="record that the shared lock was busy and exit 75",
    )
    args = parser.parse_args(argv)
    if args.record_skip:
        write_status(args.status_dir, "full_backup", "SKIPPED", reason="locked")
        print(status_line("full_backup", "SKIPPED", "locked"))
        return 75
    try:
        archive = run(
            args.data,
            args.backup_dir,
            settings=load_settings(args.config),
            snapshot_runner=snapshot_runner,
        )
    except BaseException as error:  # noqa: BLE001 — status must always be emitted
        write_status(args.status_dir, "full_backup", "FAILED", reason=str(error))
        print(status_line("full_backup", "FAILED", str(error)), file=sys.stderr)
        return 1
    write_status(args.status_dir, "full_backup", "OK", backup_path=str(archive))
    print(status_line("full_backup", "OK", f"path={archive}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
