import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_backup.essential_backup import run
from hermes_backup.hashing import write_sha256sums
from hermes_backup.restore_drill import DrillError, drill
from tests.backup.test_essential_backup import _direct_runner, _fixture_tree

REPO = Path(__file__).resolve().parents[2]
PLIST = REPO / "deploy" / "macos" / "com.hermes.restore-drill.plist"
WRAPPER = REPO / "deploy" / "macos" / "hermes_restore_drill.sh"


def _published(tmp_path: Path) -> Path:
    """A real backup, built by the real orchestrator with in-process snapshots."""
    return run(
        _fixture_tree(tmp_path),
        tmp_path / "essential",
        snapshot_runner=_direct_runner,
    )


def _rewrite(published: Path, mutate) -> Path:
    """Rebuild a published backup after mutating its extracted tree or STATE.

    Checksums are recomputed, so the result is a *valid* backup that differs
    only in what the mutation changed — otherwise every test would fail on
    the manifest instead of on the property under test.
    """
    workdir = Path(tempfile.mkdtemp())
    tree = workdir / "tree"
    with tarfile.open(published / "essential.tar.gz") as tar:
        tar.extractall(tree, filter="tar")
    mutate(tree, published)
    (published / "essential.tar.gz").unlink()
    with tarfile.open(published / "essential.tar.gz", "w:gz") as tar:
        for item in sorted(tree.rglob("*")):
            tar.add(item, arcname=item.relative_to(tree).as_posix(), recursive=False)
    (published / "essential.tar.gz").chmod(0o600)
    write_sha256sums(published)
    shutil.rmtree(workdir, ignore_errors=True)
    return published


def _set_state(published: Path, key: str, value: str) -> None:
    lines = (published / "STATE").read_text().splitlines()
    updated = [
        f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines
    ]
    (published / "STATE").write_text("\n".join(updated) + "\n")
    write_sha256sums(published)


def test_healthy_backup_passes_and_reports_counts(tmp_path):
    summary = drill(_published(tmp_path))
    assert summary["sessions"] == 2
    assert summary["skills"] == 1
    assert summary["plugins"] == 1
    assert summary["cron_jobs"] == 1
    assert summary["unclassified"] >= 0


def test_temporary_directory_is_removed(tmp_path, monkeypatch):
    seen = {}
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args, **kwargs):
        seen["path"] = real_mkdtemp(*args, **kwargs)
        return seen["path"]

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    drill(_published(tmp_path))
    assert not Path(seen["path"]).exists()


def test_a_directory_that_cannot_be_removed_fails_the_drill(tmp_path, monkeypatch):
    """The temporary tree holds live tokens: leaving it behind is a failure."""
    import hermes_backup.restore_drill as module

    # Build the backup first: module.shutil is the shutil module itself, so
    # stubbing rmtree would also stop the orchestrator cleaning its staging.
    published = _published(tmp_path)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *a, **k: None)
    with pytest.raises(DrillError, match="cleanup_failed"):
        drill(published)


def test_stale_created_at_is_rejected(tmp_path):
    published = _published(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _set_state(published, "CREATED_AT", old)
    with pytest.raises(DrillError, match="stale_backup"):
        drill(published)


def test_local_mtime_does_not_make_an_old_backup_look_fresh(tmp_path):
    published = _published(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _set_state(published, "CREATED_AT", old)
    os.utime(published, None)  # touched right now
    with pytest.raises(DrillError, match="stale_backup"):
        drill(published)


def test_checksum_mismatch_is_rejected(tmp_path):
    published = _published(tmp_path)
    (published / "STATE").write_text("BACKUP_FORMAT_VERSION=1\n")
    with pytest.raises(DrillError, match="checksum|manifest|state"):
        drill(published)


def test_an_extra_file_in_the_directory_is_rejected(tmp_path):
    published = _published(tmp_path)
    (published / "surprise.txt").write_text("x")
    with pytest.raises(DrillError, match="unexpected"):
        drill(published)


def test_unknown_backup_format_version_is_rejected(tmp_path):
    published = _published(tmp_path)
    _set_state(published, "BACKUP_FORMAT_VERSION", "2")
    with pytest.raises(DrillError, match="format_version"):
        drill(published)


def test_corrupt_database_is_caught(tmp_path):
    def break_db(tree: Path, published: Path) -> None:
        (tree / "state.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)

    published = _rewrite(_published(tmp_path), break_db)
    with pytest.raises(DrillError, match="integrity|sha256"):
        drill(published)


def test_database_sha_mismatch_is_caught(tmp_path):
    """A readable database that is not the one STATE recorded is a failure."""

    def rewrite_db(tree: Path, published: Path) -> None:
        connection = sqlite3.connect(tree / "kanban.db")
        connection.execute("INSERT INTO t (id) VALUES (99)")
        connection.commit()
        connection.close()

    published = _rewrite(_published(tmp_path), rewrite_db)
    with pytest.raises(DrillError, match="KANBAN_DB_SHA256|page_count"):
        drill(published)


def test_counter_mismatch_is_caught(tmp_path):
    published = _published(tmp_path)
    _set_state(published, "EXPECTED_SKILLS", "99")
    with pytest.raises(DrillError, match="EXPECTED_SKILLS"):
        drill(published)


def test_inventory_totals_are_recomputed(tmp_path):
    published = _published(tmp_path)
    _set_state(published, "ESSENTIAL_FILE_COUNT", "9999")
    with pytest.raises(DrillError, match="ESSENTIAL_FILE_COUNT"):
        drill(published)


def test_zero_cron_jobs_is_valid(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "cron" / "jobs.json").write_text('{"jobs": []}')
    published = run(data, tmp_path / "essential", snapshot_runner=_direct_runner)
    assert drill(published)["cron_jobs"] == 0


def test_a_required_path_that_is_not_a_regular_file_is_rejected(tmp_path):
    def replace_with_directory(tree: Path, published: Path) -> None:
        (tree / "auth.json").unlink()
        (tree / "auth.json").mkdir()

    published = _rewrite(_published(tmp_path), replace_with_directory)
    with pytest.raises(DrillError, match="not_a_regular_file|missing_required"):
        drill(published)


def test_world_readable_secret_is_rejected(tmp_path):
    def loosen(tree: Path, published: Path) -> None:
        (tree / "auth.json").chmod(0o644)

    published = _rewrite(_published(tmp_path), loosen)
    with pytest.raises(DrillError, match="permissions_too_wide"):
        drill(published)


def test_world_readable_env_file_is_rejected(tmp_path):
    def loosen(tree: Path, published: Path) -> None:
        # Loosen the existing .env rather than adding one: a new file would
        # change the inventory totals and fail that check first.
        (tree / ".env").chmod(0o644)

    published = _rewrite(_published(tmp_path), loosen)
    with pytest.raises(DrillError, match="permissions_too_wide"):
        drill(published)


def test_broken_config_yields_a_failed_status_not_a_traceback(tmp_path):
    from hermes_backup.restore_drill import main

    published = _published(tmp_path)
    broken = tmp_path / "config.yaml"
    broken.write_text("backup: [unclosed\n")
    status_dir = tmp_path / "status"

    code = main([
        "--backup",
        str(published),
        "--status-dir",
        str(status_dir),
        "--config",
        str(broken),
    ])

    assert code == 1
    from hermes_backup.status import read_status

    assert read_status(status_dir, "restore_drill")["outcome"] == "FAILED"


def test_drill_makes_no_network_or_container_calls(tmp_path):
    """Stub docker/ssh/curl so any call aborts, then run the real drill."""
    published = _published(tmp_path)
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    for name in ("docker", "ssh", "curl", "rsync"):
        stub = stub_dir / name
        stub.write_text('#!/bin/sh\necho "forbidden call: $0" >&2\nexit 99\n')
        stub.chmod(0o755)

    env = dict(os.environ, PATH=f"{stub_dir}:/usr/bin:/bin")
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_backup.restore_drill",
            "--backup",
            str(published),
            "--status-dir",
            str(tmp_path / "status"),
            "--config",
            str(tmp_path / "absent.yaml"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "forbidden call" not in result.stderr


def test_drill_runs_on_sunday_morning():
    data = plistlib.loads(PLIST.read_bytes())
    assert data["StartCalendarInterval"] == {"Weekday": 0, "Hour": 11, "Minute": 0}
    assert "EnvironmentVariables" not in data
    assert "PYTHONPATH" not in PLIST.read_text()
    assert "/.local/share/hermes/operations-runtime/run.sh" in PLIST.read_text()
    assert "/Documents/" not in PLIST.read_text()


def test_wrapper_locates_the_repository_relative_to_itself():
    text = WRAPPER.read_text()
    assert "BASH_SOURCE" in text
    assert 'cd "$REPO"' in text
    assert ".venv/bin/python" in text
    assert "HERMES_REPO" not in text


def test_a_substituted_inventory_is_caught(tmp_path):
    """Totals can be preserved while every checksum in the file is a lie."""
    published = _published(tmp_path)
    rows = [
        json.loads(line)
        for line in (published / "INVENTORY.jsonl").read_text().splitlines()
    ]
    rows[0]["sha256"] = "f" * 64
    (published / "INVENTORY.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    write_sha256sums(published)

    with pytest.raises(DrillError, match="inventory_mismatch"):
        drill(published)


def test_an_executable_secret_is_rejected(tmp_path):
    """0700 is not "no wider than 0600" — it is a mode no secret should have."""

    def loosen(tree: Path, published: Path) -> None:
        (tree / "auth.json").chmod(0o700)

    published = _rewrite(_published(tmp_path), loosen)
    with pytest.raises(DrillError, match="permissions_too_wide"):
        drill(published)


def test_an_unparsable_created_at_is_a_drill_error(tmp_path):
    published = _published(tmp_path)
    _set_state(published, "CREATED_AT", "not-a-date")
    with pytest.raises(DrillError, match="created_at_invalid"):
        drill(published)


def _token_store(tree: Path, mode: int = 0o700, file_mode: int = 0o600) -> Path:
    store = tree / "mcp-tokens"
    store.mkdir(parents=True, exist_ok=True)
    token = store / "vercel.json"
    token.write_text('{"access_token": "x"}')
    token.chmod(file_mode)
    store.chmod(mode)
    return store


def test_a_healthy_token_store_passes(tmp_path):
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    _token_store(tree)
    _check_token_store(tree)


def test_a_missing_token_store_is_allowed(tmp_path):
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    _check_token_store(tree)


def test_a_world_readable_token_is_rejected(tmp_path):
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    _token_store(tree, file_mode=0o644)
    with pytest.raises(DrillError, match="permissions_too_wide"):
        _check_token_store(tree)


def test_a_group_readable_token_directory_is_rejected(tmp_path):
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    _token_store(tree, mode=0o755)
    with pytest.raises(DrillError, match="token_store_mode"):
        _check_token_store(tree)


def test_a_nested_token_is_checked_too(tmp_path):
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    store = _token_store(tree)
    nested = store / "provider" / "refresh.json"
    nested.parent.mkdir()
    nested.write_text("{}")
    nested.chmod(0o640)
    # The directory itself is fine, so the file's mode is what must fail.
    nested.parent.chmod(0o700)
    with pytest.raises(DrillError, match="permissions_too_wide"):
        _check_token_store(tree)


def test_a_symlink_inside_the_token_store_is_rejected(tmp_path):
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    store = _token_store(tree)
    (store / "alias.json").symlink_to("vercel.json")
    with pytest.raises(DrillError, match="token_store_symlink"):
        _check_token_store(tree)


def test_an_unparsable_token_is_rejected(tmp_path):
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    store = _token_store(tree)
    broken = store / "broken.json"
    broken.write_text("{not json")
    broken.chmod(0o600)
    with pytest.raises(DrillError, match="token_unparsable"):
        _check_token_store(tree)


def test_a_token_store_that_is_a_symlink_is_rejected(tmp_path):
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tree / "mcp-tokens").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(DrillError, match="token_store_not_a_directory"):
        _check_token_store(tree)


def test_a_dangling_token_store_symlink_is_rejected(tmp_path):
    """exists() says False for a dangling link — it must not read as absent."""
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "mcp-tokens").symlink_to(tmp_path / "nowhere")
    with pytest.raises(DrillError, match="token_store_not_a_directory"):
        _check_token_store(tree)


def test_a_group_readable_nested_directory_is_rejected(tmp_path):
    """A readable directory leaks the file names, which name the providers."""
    from hermes_backup.restore_drill import _check_token_store

    tree = tmp_path / "tree"
    tree.mkdir()
    store = _token_store(tree)
    nested = store / "provider"
    nested.mkdir()
    (nested / "refresh.json").write_text("{}")
    (nested / "refresh.json").chmod(0o600)
    nested.chmod(0o755)
    with pytest.raises(DrillError, match="token_store_mode"):
        _check_token_store(tree)


def test_a_loose_token_fails_the_whole_drill(tmp_path):
    """End to end: a real backup with a 0644 token must fail the real drill."""
    data = _fixture_tree(tmp_path)
    store = data / "mcp-tokens"
    store.mkdir()
    token = store / "vercel.json"
    token.write_text('{"access_token": "x"}')
    token.chmod(0o644)
    store.chmod(0o700)

    published = run(data, tmp_path / "essential", snapshot_runner=_direct_runner)

    with pytest.raises(DrillError, match="permissions_too_wide mcp-tokens/vercel.json"):
        drill(published)


def test_a_loose_historical_config_fails_the_whole_drill(tmp_path):
    """config.yaml.bak-* carries the same provider keys as the live one."""
    data = _fixture_tree(tmp_path)
    backup = data / "config.yaml.bak-20260715T214953Z"
    backup.write_text("model: opus\n")
    backup.chmod(0o640)

    published = run(data, tmp_path / "essential", snapshot_runner=_direct_runner)

    with pytest.raises(
        DrillError, match="permissions_too_wide config.yaml.bak-20260715T214953Z"
    ):
        drill(published)


def test_a_private_historical_config_passes(tmp_path):
    data = _fixture_tree(tmp_path)
    backup = data / "config.yaml.pre-cutover"
    backup.write_text("model: opus\n")
    backup.chmod(0o600)

    published = run(data, tmp_path / "essential", snapshot_runner=_direct_runner)

    assert drill(published)["sessions"] == 2


def test_a_symlinked_secret_is_rejected(tmp_path):
    from hermes_backup.restore_drill import _require_private_modes

    tree = tmp_path / "tree"
    tree.mkdir()
    (tmp_path / "elsewhere").write_text("secrets")
    (tree / "auth.json").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(DrillError, match="secret_not_a_regular_file auth.json"):
        _require_private_modes(tree)


def test_a_dangling_secret_symlink_is_rejected(tmp_path):
    from hermes_backup.restore_drill import _require_private_modes

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".env").symlink_to(tmp_path / "nowhere")
    with pytest.raises(DrillError, match="secret_not_a_regular_file .env"):
        _require_private_modes(tree)


def test_a_directory_in_place_of_a_secret_is_rejected(tmp_path):
    from hermes_backup.restore_drill import _require_private_modes

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "config.yaml").mkdir()
    with pytest.raises(DrillError, match="secret_not_a_regular_file config.yaml"):
        _require_private_modes(tree)
