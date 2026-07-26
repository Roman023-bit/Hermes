import hashlib
import json
import plistlib
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_backup.offsite_pull import (
    BACKUP_FILES,
    check_freshness,
    prune,
    pull,
    verify_backup,
)

REPO = Path(__file__).resolve().parents[2]
PLIST = REPO / "deploy" / "macos" / "com.hermes.offsite-pull.plist"
WRAPPER = REPO / "deploy" / "macos" / "hermes_pull_offsite.sh"
STAMP = "20260726T031500Z"


def _state_text(created_at: str) -> str:
    values = {
        "BACKUP_FORMAT_VERSION": 1,
        "CREATED_AT": created_at,
        "SOURCE_HOST": "aeza",
        "HERMES_GIT_SHA": "abc1234",
        "HERMES_IMAGE_ID": "sha256:abc",
        "HERMES_IMAGE_REF": "hermes:latest",
        "STATE_DB_SHA256": "a" * 64,
        "STATE_DB_PAGE_COUNT": 10,
        "STATE_DB_USER_VERSION": 0,
        "KANBAN_DB_SHA256": "b" * 64,
        "KANBAN_DB_PAGE_COUNT": 2,
        "KANBAN_DB_USER_VERSION": 0,
        "EXPECTED_SESSIONS": 2,
        "EXPECTED_SKILLS": 78,
        "EXPECTED_PLUGINS": 3,
        "EXPECTED_CRON_JOBS": 4,
        "ESSENTIAL_FILE_COUNT": 900,
        "ESSENTIAL_TOTAL_BYTES": 1000,
        "UNCLASSIFIED_FILE_COUNT": 0,
        "EXCLUDED_SPECIAL_COUNT": 0,
        "EXCLUDED_ESCAPING_LINK_COUNT": 0,
    }
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def _make_backup(directory: Path, created_at: str | None = None) -> Path:
    """A well-formed backup directory, exactly as the server publishes it."""
    directory.mkdir(parents=True)
    created_at = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "essential.tar.gz": b"archive-bytes",
        "STATE": _state_text(created_at).encode(),
        "INVENTORY.jsonl": b'{"path": "auth.json", "type": "file"}\n',
        "EXCLUSIONS.jsonl": b'{"path": "cache/x", "type": "file"}\n',
    }
    lines = []
    for name, blob in payload.items():
        (directory / name).write_bytes(blob)
        (directory / name).chmod(0o600)
        lines.append(f"{hashlib.sha256(blob).hexdigest()}  {name}\n")
    (directory / "SHA256SUMS").write_text("".join(sorted(lines)))
    (directory / "SHA256SUMS").chmod(0o600)
    directory.chmod(0o700)
    return directory


class _Runner:
    """Stands in for ssh and rsync, and remembers what was called."""

    def __init__(self, fixture: Path, *, ssh_code: int = 0, rsync_code: int = 0):
        self.fixture = fixture
        self.ssh_code = ssh_code
        self.rsync_code = rsync_code
        self.calls: list[str] = []

    def __call__(self, argv, **kwargs):
        program = Path(argv[0]).name
        self.calls.append(program)
        if program == "ssh":
            return subprocess.CompletedProcess(
                argv,
                self.ssh_code,
                stdout=f"daily-{STAMP}\n" if self.ssh_code == 0 else "",
                stderr="" if self.ssh_code == 0 else "ssh: connect timed out",
            )
        destination = Path(argv[-1])
        if self.rsync_code == 0:
            shutil.copytree(self.fixture, destination, dirs_exist_ok=True)
        return subprocess.CompletedProcess(
            argv,
            self.rsync_code,
            stdout="",
            stderr=""
            if self.rsync_code == 0
            else "rsync: connection unexpectedly closed",
        )


def test_pull_publishes_atomically_with_private_modes(tmp_path):
    fixture = _make_backup(tmp_path / "fixture")
    root = tmp_path / "offsite"
    runner = _Runner(fixture)

    published = pull(root, "root@host", tmp_path / "key", runner=runner)

    assert published.name == f"daily-{STAMP}"
    assert not list(root.glob(".*partial"))
    assert published.stat().st_mode & 0o777 == 0o700
    for name in BACKUP_FILES:
        assert (published / name).stat().st_mode & 0o777 == 0o600
    assert runner.calls == ["ssh", "rsync"]


def test_ssh_failure_is_reported_and_publishes_nothing(tmp_path):
    fixture = _make_backup(tmp_path / "fixture")
    root = tmp_path / "offsite"
    runner = _Runner(fixture, ssh_code=255)

    with pytest.raises(RuntimeError, match="ssh_failed"):
        pull(root, "root@host", tmp_path / "key", runner=runner)

    assert runner.calls == ["ssh"]
    assert not list(root.glob("daily-*"))


def test_rsync_failure_carries_stderr_and_leaves_no_visible_backup(tmp_path):
    fixture = _make_backup(tmp_path / "fixture")
    root = tmp_path / "offsite"
    runner = _Runner(fixture, rsync_code=12)

    with pytest.raises(RuntimeError, match="connection unexpectedly closed"):
        pull(root, "root@host", tmp_path / "key", runner=runner)

    assert not list(root.glob("daily-*"))
    assert not list(root.glob(".*partial"))


def test_a_partial_manifest_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    lines = (directory / "SHA256SUMS").read_text().splitlines()[:3]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n")

    with pytest.raises(RuntimeError, match="manifest"):
        verify_backup(directory)


def test_path_traversal_in_the_manifest_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    text = (directory / "SHA256SUMS").read_text()
    (directory / "SHA256SUMS").write_text(text + f"{'c' * 64}  ../escape\n")

    with pytest.raises(RuntimeError, match="manifest"):
        verify_backup(directory)


def test_a_bogus_digest_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    lines = (directory / "SHA256SUMS").read_text().splitlines()
    lines[0] = "not-a-digest  STATE"
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n")

    with pytest.raises(RuntimeError, match="manifest"):
        verify_backup(directory)


def test_an_extra_file_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    (directory / "surprise.txt").write_text("x")

    with pytest.raises(RuntimeError, match="unexpected"):
        verify_backup(directory)


def test_a_symlinked_member_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    (directory / "STATE").unlink()
    (directory / "STATE").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(RuntimeError, match="regular file"):
        verify_backup(directory)


def test_freshness_uses_created_at_not_local_mtime(tmp_path):
    """A week-old archive pulled today is stale, however new the directory."""
    root = tmp_path / "offsite"
    old = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _make_backup(root / f"daily-{STAMP}", created_at=old)

    with pytest.raises(RuntimeError, match="stale_backup"):
        check_freshness(root, 26)


def test_freshness_accepts_a_recent_created_at(tmp_path):
    root = tmp_path / "offsite"
    directory = _make_backup(root / f"daily-{STAMP}")
    assert check_freshness(root, 26) == directory


def test_freshness_rejects_an_empty_root(tmp_path):
    with pytest.raises(RuntimeError, match="no_backup"):
        check_freshness(tmp_path, 26)


def test_partial_directories_are_invisible(tmp_path):
    (tmp_path / f".daily-{STAMP}.partial").mkdir()
    with pytest.raises(RuntimeError, match="no_backup"):
        check_freshness(tmp_path, 26)


def test_prune_keeps_the_floor(tmp_path):
    for day in range(1, 11):
        (tmp_path / f"daily-2026072{day % 10}T0{day}1500Z").mkdir()
    prune(tmp_path, keep=7, floor=2)
    assert len(list(tmp_path.glob("daily-*"))) == 7


def test_prune_never_empties_the_directory(tmp_path):
    (tmp_path / f"daily-{STAMP}").mkdir()
    prune(tmp_path, keep=0, floor=2)
    assert len(list(tmp_path.glob("daily-*"))) == 1


def test_filevault_off_makes_no_network_call(tmp_path, monkeypatch):
    import hermes_backup.offsite_pull as module

    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "require_filevault",
        lambda: (_ for _ in ()).throw(module.FileVaultOff("filevault_off")),
    )
    monkeypatch.setattr(
        module, "pull", lambda *a, **k: calls.append("pull") or Path("/nowhere")
    )

    code = module.main([
        "--root",
        str(tmp_path / "offsite"),
        "--status-dir",
        str(tmp_path / "status"),
        "--config",
        str(tmp_path / "absent.yaml"),
    ])

    assert code == 1
    assert calls == []


def test_launch_agent_carries_no_environment():
    data = plistlib.loads(PLIST.read_bytes())
    assert data["Label"] == "com.hermes.offsite-pull"
    assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 0}
    # The wrapper finds the repository itself; env in a plist goes stale.
    assert "EnvironmentVariables" not in data
    text = PLIST.read_text()
    assert "PYTHONPATH" not in text
    assert "HERMES_REPO" not in text


def test_wrapper_locates_the_repository_relative_to_itself():
    text = WRAPPER.read_text()
    assert "BASH_SOURCE" in text
    assert 'cd "$REPO"' in text
    assert ".venv/bin/python" in text
    assert "PYTHONPATH" not in text


def test_an_unparsable_created_at_is_reported(tmp_path):
    root = tmp_path / "offsite"
    directory = _make_backup(root / f"daily-{STAMP}", created_at="whenever")
    lines = [
        line
        for line in (directory / "STATE").read_text().splitlines()
        if not line.startswith("CREATED_AT=")
    ]
    (directory / "STATE").write_text("\n".join(lines + ["CREATED_AT=whenever"]) + "\n")
    digest = hashlib.sha256((directory / "STATE").read_bytes()).hexdigest()
    manifest = [
        f"{digest}  STATE" if line.endswith("  STATE") else line
        for line in (directory / "SHA256SUMS").read_text().splitlines()
    ]
    (directory / "SHA256SUMS").write_text("\n".join(manifest) + "\n")

    with pytest.raises(RuntimeError, match="created_at_invalid"):
        check_freshness(root, 26)


def test_non_utf8_state_is_refused_not_raised(tmp_path):
    """Corrupted bytes must fail the check, not crash the caller."""
    directory = _make_backup(tmp_path / "daily-x")
    (directory / "STATE").write_bytes(b"CREATED_AT=2026-07-26T03:15:00Z\n\xff\xfe\n")
    digest = hashlib.sha256((directory / "STATE").read_bytes()).hexdigest()
    manifest = [
        f"{digest}  STATE" if line.endswith("  STATE") else line
        for line in (directory / "SHA256SUMS").read_text().splitlines()
    ]
    (directory / "SHA256SUMS").write_text("\n".join(manifest) + "\n")

    with pytest.raises(RuntimeError, match="unreadable STATE"):
        verify_backup(directory)
