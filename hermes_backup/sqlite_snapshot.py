"""Consistent snapshots of a live SQLite database.

VACUUM INTO writes a transactionally consistent copy while the database
keeps serving writes, so the backup never has to stop Hermes and never
catches a main file and its -wal at different points.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


class SnapshotError(RuntimeError):
    """A database could not be snapshotted or failed its own checks."""


def _connect(db: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{db}?mode=rw", uri=True)
    except sqlite3.Error as error:
        raise SnapshotError(f"{db}: {error}") from error


def snapshot(src: Path, dest: Path) -> None:
    if dest.exists():
        raise SnapshotError(f"{dest}: destination exists")
    with closing(_connect(src)) as connection:
        try:
            connection.execute("VACUUM INTO ?", (str(dest),))
        except sqlite3.Error as error:
            raise SnapshotError(f"{src}: {error}") from error
    dest.chmod(0o600)


def _scalar(db: Path, statement: str):
    with closing(_connect(db)) as connection:
        try:
            return connection.execute(statement).fetchone()
        except sqlite3.Error as error:
            raise SnapshotError(f"{db}: {error}") from error


def integrity_check(db: Path) -> None:
    row = _scalar(db, "PRAGMA integrity_check")
    if not row or row[0] != "ok":
        raise SnapshotError(f"{db}: integrity_check reported {row}")


def foreign_key_check(db: Path) -> None:
    with closing(_connect(db)) as connection:
        try:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as error:
            raise SnapshotError(f"{db}: {error}") from error
    if violations:
        raise SnapshotError(f"{db}: {len(violations)} foreign key violation(s)")


def page_count(db: Path) -> int:
    return int(_scalar(db, "PRAGMA page_count")[0])


def user_version(db: Path) -> int:
    return int(_scalar(db, "PRAGMA user_version")[0])
