#!/usr/bin/env python3
"""Snapshot databases into a directory.

Runs as a separate process so the orchestrator can drop privileges for
this step alone: the databases belong to the Hermes uid, and a root
connection that creates -wal/-shm would lock Hermes out of its own data.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hermes_backup.sqlite_snapshot import SnapshotError, integrity_check, snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("names", nargs="+")
    args = parser.parse_args(argv)
    args.dest.mkdir(parents=True, exist_ok=True)
    try:
        for name in args.names:
            target = args.dest / name
            snapshot(args.data / name, target)
            integrity_check(target)
    except SnapshotError as error:
        print(f"hermes_snapshot_FAILED {error}", file=sys.stderr)
        return 1
    print(f"hermes_snapshot_OK count={len(args.names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
