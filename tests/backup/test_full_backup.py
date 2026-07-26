import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest

from hermes_backup.full_backup import run
from hermes_backup.sqlite_snapshot import snapshot
from hermes_backup.status import read_status

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "beget"


def _direct_runner(uid, gid, data, dest, names):
    """Tests cannot setpriv; take the snapshots in-process instead."""
    for name in names:
        snapshot(data / name, dest / name)


def _fixture_tree(root):
    data = root / "data"
    (data / "cache").mkdir(parents=True)
    (data / "cache" / "junk.bin").write_bytes(b"0" * 32)
    (data / "sessions").mkdir()
    (data / "sessions" / "sessions.json").write_text("{}")
    (data / "config.yaml").write_text("model: opus\n")
    for name in ("state.db", "kanban.db"):
        connection = sqlite3.connect(data / name)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO t (id) VALUES (1)")
        connection.commit()
        connection.close()
    return data


def _run(data, backup_dir, **kwargs):
    kwargs.setdefault("snapshot_runner", _direct_runner)
    return run(data, backup_dir, **kwargs)


def test_archive_holds_snapshots_and_no_live_databases(tmp_path):
    data = _fixture_tree(tmp_path)
    archive = _run(data, tmp_path / "backups")

    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert "./state.db" in names or "state.db" in names
    assert "./kanban.db" in names or "kanban.db" in names
    assert not any(name.endswith(("-wal", "-shm")) for name in names)
    # The full archive keeps everything else, caches included.
    assert any("cache/junk.bin" in name for name in names)


def test_snapshot_in_the_archive_is_readable(tmp_path):
    data = _fixture_tree(tmp_path)
    archive = _run(data, tmp_path / "backups")

    with tarfile.open(archive) as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("state.db"))
        extracted = tar.extractfile(member).read()
    restored = tmp_path / "restored.db"
    restored.write_bytes(extracted)
    connection = sqlite3.connect(restored)
    assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    connection.close()


def test_retention_comes_from_config_not_the_environment(tmp_path, monkeypatch):
    from hermes_backup.config import DEFAULTS, BackupSettings

    monkeypatch.setenv("HERMES_BACKUP_KEEP", "1")
    data = _fixture_tree(tmp_path)
    backups = tmp_path / "backups"
    settings = BackupSettings(**{**DEFAULTS, "retention_server": 3})
    for _ in range(5):
        _run(data, backups, settings=settings)

    assert len(list(backups.glob("hermes-*.tar.gz"))) == 3


def test_retention_never_empties_the_directory(tmp_path):
    from hermes_backup.config import DEFAULTS, BackupSettings

    data = _fixture_tree(tmp_path)
    backups = tmp_path / "backups"
    settings = BackupSettings(**{**DEFAULTS, "retention_server": 1})
    _run(data, backups, settings=settings)
    _run(data, backups, settings=settings)

    assert len(list(backups.glob("hermes-*.tar.gz"))) == 1


def test_a_failing_snapshot_leaves_the_previous_archive_alone(tmp_path):
    data = _fixture_tree(tmp_path)
    backups = tmp_path / "backups"
    first = _run(data, backups)

    def broken(uid, gid, source, dest, names):
        raise RuntimeError("snapshot_failed (1): boom")

    with pytest.raises(RuntimeError, match="snapshot_failed"):
        _run(data, backups, snapshot_runner=broken)

    assert first.exists()
    assert not list(backups.glob("*.partial"))


def test_missing_database_fails_before_any_archive_is_written(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "kanban.db").unlink()
    backups = tmp_path / "backups"

    with pytest.raises(RuntimeError, match="missing_database"):
        _run(data, backups)

    assert not backups.exists() or not list(backups.glob("hermes-*.tar.gz"))


def test_owner_mismatch_after_the_snapshot_fails_closed(tmp_path, monkeypatch):
    """The child touched the live databases: prove it left them alone."""
    import hermes_backup.full_backup as module

    data = _fixture_tree(tmp_path)
    calls = {"n": 0}
    real = module.require_single_owner

    def drifting(paths):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("owner_mismatch: after snapshot")
        return real(paths)

    monkeypatch.setattr(module, "require_single_owner", drifting)
    with pytest.raises(RuntimeError, match="owner_mismatch"):
        _run(data, tmp_path / "backups")
    assert calls["n"] == 2


def _main_args(tmp_path, data, status_dir):
    return [
        "--data",
        str(data),
        "--backup-dir",
        str(tmp_path / "backups"),
        "--status-dir",
        str(status_dir),
        "--config",
        str(tmp_path / "absent.yaml"),
    ]


def test_status_records_success(tmp_path):
    from hermes_backup.full_backup import main

    data = _fixture_tree(tmp_path)
    status_dir = tmp_path / "status"

    code = main(_main_args(tmp_path, data, status_dir), snapshot_runner=_direct_runner)

    assert code == 0
    record = read_status(status_dir, "full_backup")
    assert record["outcome"] == "OK"
    assert record["backup_path"].endswith(".tar.gz")


def test_status_records_failure(tmp_path):
    from hermes_backup.full_backup import main

    data = _fixture_tree(tmp_path)
    (data / "state.db").unlink()
    status_dir = tmp_path / "status"

    code = main(_main_args(tmp_path, data, status_dir), snapshot_runner=_direct_runner)

    assert code == 1
    record = read_status(status_dir, "full_backup")
    assert record["outcome"] == "FAILED"
    assert "missing_database" in record["reason"]


def test_record_skip_writes_a_skipped_status_and_exits_75(tmp_path):
    from hermes_backup.full_backup import main

    status_dir = tmp_path / "status"
    code = main(_main_args(tmp_path, tmp_path / "data", status_dir) + ["--record-skip"])

    assert code == 75
    record = read_status(status_dir, "full_backup")
    assert record["outcome"] == "SKIPPED"
    assert record["reason"] == "locked"


def test_no_cli_flag_can_bypass_setpriv():
    """A production-reachable switch around privilege dropping is the one
    thing this module must not offer."""
    source = (
        Path(__file__).resolve().parents[2] / "hermes_backup" / "full_backup.py"
    ).read_text()
    assert "--in-process-snapshots" not in source


def test_snapshots_are_gone_before_the_archive_is_published(tmp_path, monkeypatch):
    """A published archive must never coexist with loose database copies."""
    import hermes_backup.full_backup as module

    seen: dict[str, bool] = {}
    real_replace = Path.replace

    def spy_replace(self, target):
        seen["snapshot_dirs"] = any(
            Path("/tmp").glob("hermes-full-snapshots-*")
        ) or bool(list(Path(tempfile.gettempdir()).glob("hermes-full-snapshots-*")))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    _run(_fixture_tree(tmp_path), tmp_path / "backups")
    assert seen["snapshot_dirs"] is False


def test_a_stray_sidecar_in_the_archive_is_rejected():
    from hermes_backup.full_backup import _require_snapshot_layout

    with pytest.raises(RuntimeError, match="sidecars"):
        _require_snapshot_layout("./config.yaml\nstate.db\nkanban.db\nstate.db-wal\n")


def test_a_duplicated_database_in_the_archive_is_rejected():
    from hermes_backup.full_backup import _require_snapshot_layout

    with pytest.raises(RuntimeError, match="copies of state.db"):
        _require_snapshot_layout("./state.db\nstate.db\nkanban.db\n")


def test_wrapper_is_a_thin_launcher_with_a_trap():
    wrapper = (DEPLOY / "backup.sh").read_text()
    assert "flock -n 9" in wrapper
    assert "/run/lock/hermes-backup.lock" in wrapper
    assert "hermes_backup.full_backup" in wrapper
    assert "trap" in wrapper
    assert "--record-skip" in wrapper
    # exec hands the process to Python: the wrapper's trap must not survive
    # to print a second, contradictory status line.
    assert "exec /usr/bin/python3" in wrapper
    assert '[ "$code" -eq 75 ]' in wrapper
    # Behaviour lives in config.yaml, not in the environment.
    assert "HERMES_BACKUP_KEEP" not in wrapper


def test_full_backup_uses_setpriv_for_snapshots():
    source = (
        Path(__file__).resolve().parents[2] / "hermes_backup" / "full_backup.py"
    ).read_text()
    assert "setpriv_runner" in source
    assert "require_single_owner" in source


def test_unit_is_hardened():
    unit = (DEPLOY / "systemd" / "hermes-full-backup.service").read_text()
    assert "SuccessExitStatus=75" in unit
    assert "UMask=0077" in unit
    assert "PrivateTmp=true" in unit
