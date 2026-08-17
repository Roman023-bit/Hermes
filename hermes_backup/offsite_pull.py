"""Pull the essential backup to the Mac and check what landed.

Pull, never push: the server has no route into this laptop. The local
publish is atomic too — a half-transferred directory must never look
like a backup the drill can pick.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup import config
from hermes_backup.config import ConfigError, load_settings
from hermes_backup.filevault import FileVaultOff, require_filevault
from hermes_backup.hashing import sha256_file
from hermes_backup.locks import FileLock, LockBusy, LockTimeout
from hermes_backup.state import StateError, parse_state
from hermes_backup.status import status_line, write_status

BACKUP_FILES = frozenset({
    "essential.tar.gz",
    "STATE",
    "INVENTORY.jsonl",
    "EXCLUSIONS.jsonl",
    "SHA256SUMS",
})
MANIFEST_NAME = "SHA256SUMS"
STAMP = re.compile(r"\Adaily-[0-9]{8}T[0-9]{6}Z\Z")
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")


def _ssh_command(key: Path, bind_interface: str | None = None) -> str:
    bind = f"-o BindInterface={bind_interface} " if bind_interface else ""
    return (
        f"ssh {bind}-o BatchMode=yes -o ConnectTimeout=15 "
        f"-o ServerAliveInterval=15 -o ServerAliveCountMax=12 -i {key}"
    )


def _bind_context(bind_interface: str | None) -> str:
    """Describe the selected route without weakening fail-closed behavior."""
    return f" bind_interface={bind_interface}" if bind_interface else ""


def _read_text(path: Path) -> str:
    """Read a backup file as text, refusing rather than raising.

    These bytes arrived over the network: a truncated or corrupted file can
    be invalid UTF-8, and a UnicodeDecodeError escaping from here would take
    down whichever caller asked — including the status summary, whose whole
    job is to describe a broken backup instead of dying with it.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError(f"unreadable {path.name}: {error}") from error


def verify_backup(directory: Path) -> dict:
    """Prove the directory is a complete, self-consistent backup.

    Everything here is checked before the directory becomes visible as a
    backup: a truncated transfer that happens to carry a valid STATE must
    not be mistaken for a copy worth restoring from.
    """
    entries = list(directory.iterdir())
    names = {entry.name for entry in entries}
    if names != set(BACKUP_FILES):
        raise RuntimeError(f"unexpected contents: {sorted(names ^ set(BACKUP_FILES))}")
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise RuntimeError(f"not a regular file: {entry.name}")

    listed: set[str] = set()
    for line in _read_text(directory / MANIFEST_NAME).splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or not _DIGEST.match(digest):
            raise RuntimeError(f"manifest line malformed: {line!r}")
        if (
            "/" in name
            or name in {"..", "."}
            or name not in BACKUP_FILES - {MANIFEST_NAME}
        ):
            raise RuntimeError(f"manifest names an unexpected file: {name!r}")
        if name in listed:
            raise RuntimeError(f"manifest lists {name!r} twice")
        listed.add(name)
        if sha256_file(directory / name) != digest:
            raise RuntimeError(f"checksum_mismatch {name}")
    if listed != BACKUP_FILES - {MANIFEST_NAME}:
        raise RuntimeError(
            f"manifest incomplete: missing {sorted(BACKUP_FILES - {MANIFEST_NAME} - listed)}"
        )

    try:
        return parse_state(_read_text(directory / "STATE"))
    except StateError as error:
        raise RuntimeError(f"state_invalid {error}") from error


def _latest_remote(
    remote: str,
    key: Path,
    remote_root: str,
    runner,
    bind_interface: str | None = None,
) -> str:
    bind_options = (
        ["-o", f"BindInterface={bind_interface}"] if bind_interface else []
    )
    result = runner(
        [
            "ssh",
            *bind_options,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-i",
            str(key),
            remote,
            f"find '{remote_root}' -mindepth 1 -maxdepth 1 -type d -name 'daily-*' "
            "-printf '%f\\n' | LC_ALL=C sort | tail -1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ssh_failed ({result.returncode}){_bind_context(bind_interface)}: "
            f"{result.stderr.strip()}"
        )
    name = result.stdout.strip()
    if not STAMP.match(name):
        raise RuntimeError(f"invalid_remote_name {name!r}")
    return name


def _apply_modes(directory: Path) -> None:
    directory.chmod(0o700)
    for item in directory.iterdir():
        item.chmod(0o600)


def pull(
    root: Path,
    remote: str,
    key: Path,
    remote_root: str = "/srv/hermes/backups/essential",
    runner=subprocess.run,
    bind_interface: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    name = _latest_remote(remote, key, remote_root, runner, bind_interface)
    published = root / name
    if published.exists():
        verify_backup(published)
        return published

    partial = root / f".{name}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(mode=0o700)
    try:
        result = runner(
            [
                "rsync",
                "-a",
                "--partial",
                "-e",
                _ssh_command(key, bind_interface),
                f"{remote}:{remote_root}/{name}/",
                f"{partial}/",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"rsync_failed ({result.returncode})"
                f"{_bind_context(bind_interface)}: {result.stderr.strip()}"
            )
        _apply_modes(partial)
        # Verify before the rename: nothing may become visible as a backup
        # until it has proven itself complete.
        verify_backup(partial)
        partial.rename(published)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return published


def check_freshness(root: Path, max_age_hours: int) -> Path:
    backups = sorted(item for item in root.glob("daily-*") if item.is_dir())
    if not backups:
        raise RuntimeError("no_backup")
    newest = backups[-1]
    state = verify_backup(newest)
    # CREATED_AT, not mtime: mtime says when we downloaded it, and a
    # week-old archive fetched today would look brand new.
    try:
        created = datetime.strptime(
            str(state["CREATED_AT"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise RuntimeError(f"created_at_invalid {state['CREATED_AT']!r}") from error
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise RuntimeError(f"stale_backup age_hours={age_hours:.1f}")
    return newest


def prune(root: Path, keep: int, floor: int) -> None:
    backups = sorted(item for item in root.glob("daily-*") if item.is_dir())
    keep = max(keep, floor)
    for stale in backups[: max(0, len(backups) - keep)]:
        shutil.rmtree(stale, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=config.MAC_OFFSITE_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.MAC_STATUS_DIR)
    parser.add_argument("--config", type=Path, default=config.MAC_CONFIG)
    parser.add_argument(
        "--bind-interface",
        default=config.MAC_SSH_BIND_INTERFACE,
        help=(
            "macOS interface used by SSH and rsync "
            f"(default: {config.MAC_SSH_BIND_INTERFACE})"
        ),
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        require_filevault()
    except (FileVaultOff, ConfigError) as error:
        write_status(args.status_dir, "offsite_pull", "FAILED", reason=str(error))
        print(status_line("offsite_pull", "FAILED", str(error)), file=sys.stderr)
        return 1

    lock = FileLock(config.MAC_NETWORK_LOCK, owner="hermes-pull")
    try:
        lock.acquire(wait_seconds=settings.network_lock_wait_seconds)
    except (LockBusy, LockTimeout) as error:
        write_status(args.status_dir, "offsite_pull", "FAILED", reason="lock_timeout")
        print(
            status_line("offsite_pull", "FAILED", f"lock_timeout {error}"),
            file=sys.stderr,
        )
        return 1
    try:
        published = pull(
            args.root,
            config.REMOTE,
            config.SSH_KEY,
            bind_interface=args.bind_interface,
        )
        prune(args.root, settings.retention_mac, settings.retention_mac_floor)
    except BaseException as error:  # noqa: BLE001 — status must always be emitted
        write_status(args.status_dir, "offsite_pull", "FAILED", reason=str(error))
        print(status_line("offsite_pull", "FAILED", str(error)), file=sys.stderr)
        return 1
    finally:
        lock.release()
    write_status(args.status_dir, "offsite_pull", "OK", backup_path=str(published))
    print(status_line("offsite_pull", "OK", f"path={published}"))

    try:
        fresh = check_freshness(args.root, settings.freshness_hours)
    except RuntimeError as error:
        write_status(args.status_dir, "freshness", "FAILED", reason=str(error))
        print(status_line("freshness", "FAILED", str(error)), file=sys.stderr)
        return 1
    write_status(args.status_dir, "freshness", "OK", backup_path=str(fresh))
    print(status_line("freshness", "OK", f"path={fresh}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
