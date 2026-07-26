import sqlite3

import pytest

from hermes_backup.sqlite_snapshot import (
    SnapshotError,
    foreign_key_check,
    integrity_check,
    page_count,
    snapshot,
    user_version,
)


def _make_db(path, rows=100):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connection.executemany(
        "INSERT INTO notes (body) VALUES (?)", [(f"row {n}",) for n in range(rows)]
    )
    connection.commit()
    connection.close()


def test_snapshot_of_a_wal_database_is_readable_and_complete(tmp_path):
    source = tmp_path / "state.db"
    _make_db(source)
    destination = tmp_path / "snapshot.db"

    snapshot(source, destination)

    assert not destination.with_name("snapshot.db-wal").exists()
    connection = sqlite3.connect(destination)
    assert connection.execute("SELECT count(*) FROM notes").fetchone()[0] == 100
    connection.close()


def test_snapshot_survives_writes_to_the_source_afterwards(tmp_path):
    source = tmp_path / "state.db"
    _make_db(source)
    destination = tmp_path / "snapshot.db"
    snapshot(source, destination)

    live = sqlite3.connect(source)
    live.execute("INSERT INTO notes (body) VALUES ('after snapshot')")
    live.commit()
    live.close()

    connection = sqlite3.connect(destination)
    assert connection.execute("SELECT count(*) FROM notes").fetchone()[0] == 100
    connection.close()


def test_integrity_check_rejects_a_corrupt_file(tmp_path):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises(SnapshotError):
        integrity_check(broken)


def test_pragmas_report_page_count_and_user_version(tmp_path):
    source = tmp_path / "state.db"
    _make_db(source)
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA user_version=7")
    connection.close()

    assert page_count(source) > 0
    assert user_version(source) == 7
    integrity_check(source)
    foreign_key_check(source)


def test_snapshot_refuses_to_overwrite(tmp_path):
    source = tmp_path / "state.db"
    _make_db(source)
    destination = tmp_path / "snapshot.db"
    destination.write_text("occupied")
    with pytest.raises(SnapshotError, match="exists"):
        snapshot(source, destination)
