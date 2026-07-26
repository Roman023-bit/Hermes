import json

import pytest

from hermes_backup.status import StatusError, read_status, status_line, write_status


def test_status_is_written_atomically_and_read_back(tmp_path):
    write_status(tmp_path, "essential_backup", "OK", backup_path="/srv/x/daily-1")
    record = read_status(tmp_path, "essential_backup")
    assert record["outcome"] == "OK"
    assert record["backup_path"] == "/srv/x/daily-1"
    assert record["finished_at"].endswith("Z")
    # Only files: tests/conftest.py seeds every tmp_path with hermes_test/.
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == [
        "essential_backup.json"
    ]


def test_status_directory_is_private(tmp_path):
    target = tmp_path / "status"
    write_status(target, "freshness", "OK")
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "freshness.json").stat().st_mode & 0o777 == 0o600


def test_failure_keeps_the_reason(tmp_path):
    write_status(tmp_path, "restore_drill", "FAILED", reason="integrity_check")
    assert read_status(tmp_path, "restore_drill")["reason"] == "integrity_check"


def test_missing_status_reads_as_none(tmp_path):
    assert read_status(tmp_path, "never_ran") is None


def test_malformed_status_reads_as_none(tmp_path):
    (tmp_path / "essential_backup.json").write_text("[1, 2, 3]")
    assert read_status(tmp_path, "essential_backup") is None


def test_status_missing_required_fields_reads_as_none(tmp_path):
    (tmp_path / "freshness.json").write_text(json.dumps({"outcome": "OK"}))
    assert read_status(tmp_path, "freshness") is None


@pytest.mark.parametrize(
    "name", ["../escape", "a/b", "with space", "", "x" * 65, "Upper"]
)
def test_unsafe_names_are_rejected(tmp_path, name):
    with pytest.raises(StatusError):
        write_status(tmp_path, name, "OK")


def test_unknown_outcome_is_rejected(tmp_path):
    with pytest.raises(StatusError, match="outcome"):
        write_status(tmp_path, "freshness", "MAYBE")


def test_status_line_carries_the_hermes_prefix():
    assert status_line("offsite_pull", "FAILED", "lock_timeout") == (
        "hermes_offsite_pull_FAILED lock_timeout"
    )
    assert status_line("essential_backup", "OK") == "hermes_essential_backup_OK"


def test_status_line_stays_one_line():
    """A multi-line traceback in the reason must not fake extra statuses."""
    line = status_line("restore_drill", "FAILED", "boom\nhermes_restore_drill_OK")
    assert "\n" not in line
    assert line.count("hermes_restore_drill") == 2
