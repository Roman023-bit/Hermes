import os
from pathlib import Path

import pytest

from hermes_backup.staging import (
    UnstableSourceError,
    changed_paths,
    rsync_command,
    rsync_filter,
    stabilized_copy,
)


def test_command_preserves_ownership_and_hardlinks():
    argv = rsync_command(Path("/srv/hermes/data"), Path("/tmp/staging"), dry_run=False)
    assert "-rlptgoH" in argv
    assert "--numeric-ids" in argv
    assert "--delete-excluded" in argv
    # -D would copy device nodes and FIFOs; hashing a FIFO would hang.
    assert "-a" not in argv


def test_safe_links_is_used_in_both_commands():
    """A link out of the tree would fail the archive validator later."""
    for dry_run in (False, True):
        argv = rsync_command(
            Path("/srv/hermes/data"), Path("/tmp/staging"), dry_run=dry_run
        )
        assert "--safe-links" in argv


def test_root_rules_are_anchored_for_rsync():
    assert rsync_filter("cache/*") == "/cache/"
    assert rsync_filter(".npm/*") == "/.npm/"
    assert rsync_filter("state.db-*") == "/state.db-*"
    assert (
        rsync_filter("sessions/request_dump_*.json") == "/sessions/request_dump_*.json"
    )
    assert rsync_filter("*/.DS_Store") == "**/.DS_Store"
    assert rsync_filter("**/__pycache__/*") == "**/__pycache__/"


def test_command_carries_anchored_not_bare_rules():
    argv = rsync_command(Path("/srv/hermes/data"), Path("/tmp/staging"), dry_run=False)
    assert "--exclude=/cache/" in argv
    assert "--exclude=cache/*" not in argv


def test_root_cache_goes_but_a_nested_cache_stays(tmp_path):
    source = tmp_path / "data"
    (source / "cache").mkdir(parents=True)
    (source / "cache" / "junk.bin").write_bytes(b"0" * 32)
    (source / "workspace" / "project" / "cache").mkdir(parents=True)
    (source / "workspace" / "project" / "cache" / "important.bin").write_bytes(b"1" * 8)
    staging = tmp_path / "staging"

    stabilized_copy(source, staging)

    assert not (staging / "cache").exists()
    assert (
        staging / "workspace" / "project" / "cache" / "important.bin"
    ).read_bytes() == b"1" * 8


def test_stable_tree_is_copied(tmp_path):
    source = tmp_path / "data"
    (source / "cron").mkdir(parents=True)
    (source / "cron" / "jobs.json").write_text('{"jobs": []}')
    staging = tmp_path / "staging"

    passes = stabilized_copy(source, staging)

    assert (staging / "cron" / "jobs.json").exists()
    assert passes >= 1


def test_vanished_files_are_retried_not_fatal(tmp_path, monkeypatch):
    """Exit 24 means the live tree moved under us — that is churn, not failure."""
    source = tmp_path / "data"
    source.mkdir()
    (source / "keep.txt").write_text("x")
    staging = tmp_path / "staging"
    calls = {"n": 0}

    import hermes_backup.staging as module

    real = module._run_rsync

    def flaky(source_path, staging_path, dry_run, rsync):
        calls["n"] += 1
        if calls["n"] == 1:
            raise module.VanishedFiles("exit 24")
        return real(source_path, staging_path, dry_run, rsync)

    monkeypatch.setattr(module, "_run_rsync", flaky)
    assert stabilized_copy(source, staging) >= 1
    assert calls["n"] > 1


def test_other_rsync_failures_are_immediate(tmp_path, monkeypatch):
    source = tmp_path / "data"
    source.mkdir()
    staging = tmp_path / "staging"

    import hermes_backup.staging as module

    def boom(*args, **kwargs):
        raise UnstableSourceError("rsync failed (23): permission denied")

    monkeypatch.setattr(module, "_run_rsync", boom)
    with pytest.raises(UnstableSourceError, match="23"):
        stabilized_copy(source, staging)


def test_source_that_keeps_changing_fails_closed(tmp_path, monkeypatch):
    source = tmp_path / "data"
    source.mkdir()
    churn = source / "busy.log"
    churn.write_text("0")
    staging = tmp_path / "staging"

    import hermes_backup.staging as module

    real = module._run_rsync
    counter = {"n": 0}

    def churning(source_path, staging_path, dry_run, rsync):
        counter["n"] += 1
        churn.write_text(f"{counter['n']}")
        return real(source_path, staging_path, dry_run, rsync)

    monkeypatch.setattr(module, "_run_rsync", churning)
    with pytest.raises(UnstableSourceError, match="unstable_source"):
        stabilized_copy(source, staging, attempts=2)


def test_missing_source_is_reported(tmp_path):
    with pytest.raises(UnstableSourceError):
        stabilized_copy(tmp_path / "absent", tmp_path / "staging")


def test_informational_output_is_not_mistaken_for_churn():
    """rsync writes notes to stdout; only itemized lines mean a change."""
    output = "\n".join([
        'skipping non-regular file "pipe"',
        ">f+++++++++ sessions/sessions.json",
        "*deleting   cache/junk.bin",
        "cd+++++++++ skills/",
        "",
    ])
    assert changed_paths(output) == [
        ">f+++++++++ sessions/sessions.json",
        "*deleting   cache/junk.bin",
        "cd+++++++++ skills/",
    ]


def test_a_fifo_does_not_make_the_source_look_unstable(tmp_path):
    """A socket or FIFO would otherwise fail every attempt, forever."""
    source = tmp_path / "data"
    source.mkdir()
    (source / "keep.txt").write_text("payload")
    os.mkfifo(source / "pipe")
    staging = tmp_path / "staging"

    passes = stabilized_copy(source, staging)

    assert passes >= 1
    assert (staging / "keep.txt").read_text() == "payload"
    assert not (staging / "pipe").exists()
