import pytest

from hermes_backup.state import StateError, format_state, parse_state

VALID = {
    "BACKUP_FORMAT_VERSION": 1,
    "CREATED_AT": "2026-07-26T03:15:00Z",
    "SOURCE_HOST": "aeza",
    "HERMES_GIT_SHA": "339694487",
    "HERMES_IMAGE_ID": "sha256:abc123",
    "HERMES_IMAGE_REF": "hermes:latest",
    "STATE_DB_SHA256": "a" * 64,
    "STATE_DB_PAGE_COUNT": 33776,
    "STATE_DB_USER_VERSION": 0,
    "KANBAN_DB_SHA256": "b" * 64,
    "KANBAN_DB_PAGE_COUNT": 28,
    "KANBAN_DB_USER_VERSION": 0,
    "EXPECTED_SESSIONS": 2,
    "EXPECTED_SKILLS": 78,
    "EXPECTED_PLUGINS": 3,
    "EXPECTED_CRON_JOBS": 4,
    "ESSENTIAL_FILE_COUNT": 900,
    "ESSENTIAL_TOTAL_BYTES": 152000000,
    "UNCLASSIFIED_FILE_COUNT": 0,
}


def test_round_trip_preserves_values():
    assert parse_state(format_state(VALID)) == VALID


def test_unknown_key_is_rejected():
    with pytest.raises(StateError, match="unknown key"):
        parse_state("EXPECTED_SKILLS=78\nFOO=1\n")


def test_non_numeric_value_for_int_key_is_rejected():
    with pytest.raises(StateError, match="expects an integer"):
        parse_state("EXPECTED_SKILLS=many\n")


def test_shell_substitution_is_data_not_code(tmp_path):
    canary = tmp_path / "canary"
    canary.write_text("intact")
    with pytest.raises(StateError):
        parse_state(f"SOURCE_HOST=$(rm -f {canary})\n")
    assert canary.read_text() == "intact"


def test_missing_required_key_is_rejected():
    partial = dict(VALID)
    del partial["EXPECTED_SESSIONS"]
    with pytest.raises(StateError, match="missing key"):
        parse_state(format_state(partial))


def test_duplicate_key_is_rejected():
    with pytest.raises(StateError, match="duplicate key"):
        parse_state(format_state(VALID) + "SOURCE_HOST=aeza\n")
