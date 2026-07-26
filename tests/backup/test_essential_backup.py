import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from hermes_backup.essential_backup import (
    require_single_owner,
    run,
    snapshot_command,
)
from hermes_backup.sqlite_snapshot import snapshot
from hermes_backup.state import parse_state

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "beget"


def _direct_runner(uid, gid, data, dest, names):
    """Tests cannot setpriv; take the snapshots in-process instead."""
    for name in names:
        snapshot(data / name, dest / name)


def _fixture_tree(root):
    data = root / "data"
    (data / "sessions").mkdir(parents=True)
    (data / "cron" / "state").mkdir(parents=True)
    (data / "cron" / "output").mkdir(parents=True)
    (data / "skills" / "apple" / "automation").mkdir(parents=True)
    (data / "plugins" / "image_gen" / "replicate").mkdir(parents=True)
    (data / "cache").mkdir()

    (data / "sessions" / "sessions.json").write_text(json.dumps({"a": {}, "b": {}}))
    (data / "sessions" / "request_dump_20260715_231648.json").write_text("{}")
    (data / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "x"}]}))
    (data / "cron" / "state" / "x.json").write_text("{}")
    (data / "cron" / "output" / "noise.log").write_text("noise")
    (data / "skills" / "apple" / "automation" / "SKILL.md").write_text("s")
    (data / "plugins" / "image_gen" / "replicate" / "plugin.yaml").write_text("p")
    (data / "cache" / "junk.bin").write_bytes(b"0" * 64)
    (data / "auth.json").write_text("{}")
    (data / "config.yaml").write_text("model: opus\n")
    (data / ".env").write_text("TELEGRAM_TOKEN=x\n")
    # Secrets are 0600 on the server; the drill rejects anything wider, so a
    # fixture built under the default umask would not resemble production.
    for secret in ("auth.json", "config.yaml", ".env", "sessions/sessions.json"):
        (data / secret).chmod(0o600)

    for name in ("state.db", "kanban.db"):
        connection = sqlite3.connect(data / name)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO t (id) VALUES (1)")
        connection.commit()
        connection.close()
    return data


def _run(data, root, **kwargs):
    kwargs.setdefault("snapshot_runner", _direct_runner)
    return run(data, root, **kwargs)


def test_publishes_a_directory_with_exactly_five_files(tmp_path):
    published = _run(_fixture_tree(tmp_path), tmp_path / "essential")

    assert {item.name for item in published.iterdir()} == {
        "essential.tar.gz",
        "STATE",
        "INVENTORY.jsonl",
        "EXCLUSIONS.jsonl",
        "SHA256SUMS",
    }
    assert published.name.startswith("daily-")
    assert published.stat().st_mode & 0o777 == 0o700


def test_state_counts_match_the_fixture(tmp_path):
    published = _run(_fixture_tree(tmp_path), tmp_path / "essential")

    state = parse_state((published / "STATE").read_text())
    assert state["EXPECTED_SESSIONS"] == 2
    assert state["EXPECTED_SKILLS"] == 1
    assert state["EXPECTED_PLUGINS"] == 1
    assert state["EXPECTED_CRON_JOBS"] == 1
    assert state["EXCLUDED_SPECIAL_COUNT"] == 0
    assert state["BACKUP_FORMAT_VERSION"] == 1


def test_archive_carries_snapshots_and_drops_recoverable_files(tmp_path):
    published = _run(_fixture_tree(tmp_path), tmp_path / "essential")

    with tarfile.open(published / "essential.tar.gz") as tar:
        names = set(tar.getnames())
    assert "state.db" in names and "kanban.db" in names
    assert "state.db-wal" not in names
    assert not any(name.startswith("cache/") for name in names)
    assert not any(name.startswith("cron/output/") for name in names)
    assert not any("request_dump" in name for name in names)


def test_fifo_is_counted_in_state_and_never_archived(tmp_path):
    data = _fixture_tree(tmp_path)
    os.mkfifo(data / "pipe")

    published = _run(data, tmp_path / "essential")

    state = parse_state((published / "STATE").read_text())
    assert state["EXCLUDED_SPECIAL_COUNT"] == 1
    rows = [
        json.loads(line)
        for line in (published / "EXCLUSIONS.jsonl").read_text().splitlines()
    ]
    assert any(row["classification"] == "excluded-special" for row in rows)
    with tarfile.open(published / "essential.tar.gz") as tar:
        assert "pipe" not in set(tar.getnames())


def test_torn_config_yaml_never_reaches_the_archive(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "config.yaml").write_text("model: [unclosed\n")
    root = tmp_path / "essential"

    with pytest.raises(RuntimeError, match="config_yaml"):
        _run(data, root)

    assert not list(root.glob("daily-*"))
    assert not list(root.glob(".daily-*"))


def test_empty_config_yaml_is_rejected(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "config.yaml").write_text("")
    with pytest.raises(RuntimeError, match="config_yaml"):
        _run(data, tmp_path / "essential")


def test_a_failing_snapshot_runner_publishes_nothing(tmp_path):
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"

    def broken(uid, gid, source, dest, names):
        raise RuntimeError("snapshot_failed (1): setpriv exploded")

    with pytest.raises(RuntimeError, match="snapshot_failed"):
        _run(data, root, snapshot_runner=broken)

    assert not list(root.glob("daily-*"))
    assert not list(root.glob(".daily-*"))


def test_a_silent_snapshot_runner_is_caught(tmp_path):
    """A runner that exits zero without producing files must not pass."""
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"

    with pytest.raises(RuntimeError, match="snapshot_missing"):
        _run(data, root, snapshot_runner=lambda *args: None)

    assert not list(root.glob("daily-*"))


def test_previous_backup_survives_a_failed_run(tmp_path, monkeypatch):
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"
    first = _run(data, root)

    import hermes_backup.essential_backup as module

    monkeypatch.setattr(
        module, "create", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    with pytest.raises(RuntimeError):
        _run(data, root)

    assert first.exists()
    assert (first / "SHA256SUMS").exists()


def test_missing_database_fails_before_anything_is_copied(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "kanban.db").unlink()
    root = tmp_path / "essential"

    with pytest.raises(RuntimeError, match="missing_database"):
        _run(data, root)

    assert not root.exists() or not list(root.glob(".daily-*"))


def test_split_ownership_fails_closed(tmp_path, monkeypatch):
    import hermes_backup.essential_backup as module

    first = tmp_path / "state.db"
    first.write_text("x")
    second = tmp_path / "kanban.db"
    second.write_text("x")

    real = module.owner_of
    monkeypatch.setattr(
        module,
        "owner_of",
        lambda path: (0, 0) if path.name == "kanban.db" else real(path),
    )
    with pytest.raises(RuntimeError, match="owner_mismatch"):
        require_single_owner([first, second])


def test_snapshot_child_can_traverse_the_partial_directory(tmp_path, monkeypatch):
    """A 0700 root:root parent would deny the unprivileged child."""
    import stat as stat_module

    import hermes_backup.essential_backup as module

    chowns: list[tuple[str, int, int]] = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        module.os,
        "chown",
        lambda path, uid, gid: chowns.append((Path(path).name, uid, gid)),
    )
    seen: dict[str, int] = {}

    def spy_runner(uid, gid, data, dest, names):
        seen["partial"] = stat_module.S_IMODE(dest.parent.stat().st_mode)
        seen["snapshots"] = stat_module.S_IMODE(dest.stat().st_mode)
        _direct_runner(uid, gid, data, dest, names)

    published = _run(
        _fixture_tree(tmp_path), tmp_path / "essential", snapshot_runner=spy_runner
    )

    assert seen["partial"] == 0o710
    assert seen["snapshots"] == 0o700
    assert any(name.startswith(".daily-") for name, _, _ in chowns)
    # The traversal grant must not survive the snapshot step.
    assert published.stat().st_mode & 0o777 == 0o700


def test_traversal_is_revoked_even_when_the_snapshot_fails(tmp_path, monkeypatch):
    import hermes_backup.essential_backup as module

    revoked: list[str] = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "chown", lambda *args: None)
    real_revoke = module._revoke_traversal
    monkeypatch.setattr(
        module,
        "_revoke_traversal",
        lambda partial: (revoked.append(partial.name), real_revoke(partial))[1],
    )

    def broken(uid, gid, data, dest, names):
        raise RuntimeError("snapshot_failed (1): boom")

    with pytest.raises(RuntimeError, match="snapshot_failed"):
        _run(_fixture_tree(tmp_path), tmp_path / "essential", snapshot_runner=broken)

    assert revoked


def test_snapshot_command_drops_privileges_to_the_file_owner():
    argv = snapshot_command(
        10000, 10000, Path("/srv/hermes/data"), Path("/tmp/s"), ["state.db"]
    )
    assert argv[:4] == [
        "/usr/bin/setpriv",
        "--reuid=10000",
        "--regid=10000",
        "--clear-groups",
    ]
    assert "hermes_backup.snapshot_cli" in argv
    assert argv[-1] == "state.db"


def test_publishing_twice_in_one_second_does_not_overwrite(tmp_path, monkeypatch):
    """Two runs in the same second must not silently replace each other."""
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"
    import hermes_backup.essential_backup as module

    frozen = "20260726T031500Z"
    monkeypatch.setattr(module, "_stamp", lambda: frozen)
    _run(data, root)
    with pytest.raises(RuntimeError, match="already_published"):
        _run(data, root)


def test_tree_bytes_does_not_follow_symlinks(tmp_path):
    import hermes_backup.essential_backup as module

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"0" * 4096)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "small.txt").write_bytes(b"0" * 10)
    (tree / "link").symlink_to(outside, target_is_directory=True)

    assert module._tree_bytes(tree) == 10


def test_service_treats_lock_skip_as_success():
    unit = (DEPLOY / "systemd" / "hermes-essential-backup.service").read_text()
    assert "SuccessExitStatus=75" in unit
    assert "UMask=0077" in unit


def test_wrapper_takes_the_shared_lock():
    wrapper = (DEPLOY / "hermes_essential_backup.sh").read_text()
    assert "/run/lock/hermes-backup.lock" in wrapper
    assert "flock -n 9" in wrapper
