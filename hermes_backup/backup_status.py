"""One command that answers: is the off-site copy healthy right now?"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup import config
from hermes_backup.locks import held_by
from hermes_backup.offsite_pull import verify_backup
from hermes_backup.status import read_status

COMPONENTS = ("offsite_pull", "freshness", "restore_drill")


def _describe_latest(root: Path) -> list[str]:
    backups = (
        sorted(item for item in root.glob("daily-*") if item.is_dir())
        if root.exists()
        else []
    )
    if not backups:
        return ["latest backup: no backups"]
    newest = backups[-1]
    try:
        state = verify_backup(newest)
    except RuntimeError as error:
        return [f"latest backup: {newest.name} — UNUSABLE: {error}"]
    try:
        created = datetime.strptime(
            str(state["CREATED_AT"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return [f"latest backup: {newest.name} — UNUSABLE: created_at_invalid"]
    # Age of the backup, not of the download: a week-old archive pulled a
    # minute ago is a week old.
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    return [
        f"latest backup: {newest.name} ({age_hours:.1f} h old, {len(backups)} kept)",
        f"unclassified files: {state['UNCLASSIFIED_FILE_COUNT']}",
    ]


def summary(root: Path, status_dir: Path, lock: Path) -> str:
    lines = _describe_latest(root)

    for name in COMPONENTS:
        record = read_status(status_dir, name)
        if record is None:
            lines.append(f"{name}: never ran")
            continue
        reason = f" — {record['reason']}" if record.get("reason") else ""
        lines.append(f"{name}: {record['outcome']} at {record['finished_at']}{reason}")

    # Existence proves nothing: the lock file is permanent. Ask flock.
    holder = held_by(lock)
    if holder is None:
        lines.append("network lock: free")
    elif holder:
        lines.append(
            f"network lock: held by {holder.get('owner')} since {holder.get('started_at')}"
        )
    else:
        lines.append("network lock: held (metadata unreadable)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=config.MAC_OFFSITE_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.MAC_STATUS_DIR)
    parser.add_argument("--lock", type=Path, default=config.MAC_NETWORK_LOCK)
    args = parser.parse_args(argv)
    print(summary(args.root, args.status_dir, args.lock))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
