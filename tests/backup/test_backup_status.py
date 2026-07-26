import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_backup.backup_status import summary
from hermes_backup.locks import FileLock
from hermes_backup.status import write_status
from tests.backup.test_offsite_pull import _make_backup

REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / "deploy" / "macos" / "hermes_backup_status.sh"
STAMP = "20260726T031500Z"


def test_summary_reports_every_component(tmp_path):
    root = tmp_path / "offsite"
    _make_backup(root / f"daily-{STAMP}")
    status_dir = tmp_path / "status"
    write_status(status_dir, "offsite_pull", "OK")
    write_status(status_dir, "freshness", "OK")
    write_status(
        status_dir, "restore_drill", "FAILED", reason="checksum_mismatch STATE"
    )

    text = summary(root, status_dir, tmp_path / "network.lock")

    assert f"daily-{STAMP}" in text
    assert "restore_drill: FAILED" in text
    assert "checksum_mismatch" in text
    assert "network lock: free" in text


def test_age_comes_from_created_at_not_from_the_download_time(tmp_path):
    """A week-old archive pulled a minute ago is a week old."""
    root = tmp_path / "offsite"
    old = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    directory = _make_backup(root / f"daily-{STAMP}", created_at=old)
    os.utime(directory, None)

    text = summary(root, tmp_path / "status", tmp_path / "network.lock")

    assert "168." in text or "167." in text
    assert "0.0 h" not in text


def test_unclassified_count_is_shown(tmp_path):
    root = tmp_path / "offsite"
    _make_backup(root / f"daily-{STAMP}")
    text = summary(root, tmp_path / "status", tmp_path / "network.lock")
    assert "unclassified files: 0" in text


def test_an_unusable_backup_is_named_as_such(tmp_path):
    root = tmp_path / "offsite"
    directory = _make_backup(root / f"daily-{STAMP}")
    (directory / "surprise.txt").write_text("x")

    text = summary(root, tmp_path / "status", tmp_path / "network.lock")

    assert "UNUSABLE" in text
    assert "unexpected" in text


def test_missing_components_are_named_not_hidden(tmp_path):
    text = summary(tmp_path / "offsite", tmp_path / "status", tmp_path / "network.lock")
    assert "no backups" in text
    assert "offsite_pull: never ran" in text


def test_held_lock_is_reported_with_owner(tmp_path):
    lock = tmp_path / "network.lock"
    with FileLock(lock, owner="kf-pull"):
        text = summary(tmp_path / "offsite", tmp_path / "status", lock)
    assert "kf-pull" in text
    assert "held by" in text


def test_wrapper_locates_the_repository_relative_to_itself():
    text = WRAPPER.read_text()
    assert "BASH_SOURCE" in text
    assert 'cd "$REPO"' in text
    assert ".venv/bin/python" in text
    assert "HERMES_REPO" not in text
