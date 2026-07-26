"""Lock behaviour is only meaningful across processes, so every contention
test drives a real child process rather than a second object in-process."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_backup.locks import FileLock, LockBusy, LockTimeout, held_by

REPO = Path(__file__).resolve().parents[2]

HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    from hermes_backup.locks import FileLock

    lock = FileLock(Path(sys.argv[1]), owner="child")
    lock.acquire()
    print("held", flush=True)
    time.sleep(60)
    """
)


def _holder(path: Path) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(repo=str(REPO)), str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "held"
    return process


def test_second_process_cannot_take_a_held_lock(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    try:
        with pytest.raises(LockBusy):
            FileLock(path, owner="hermes-pull").acquire()
    finally:
        holder.kill()
        holder.wait()


def test_killing_the_owner_frees_the_lock(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    holder.send_signal(signal.SIGKILL)
    holder.wait()
    # The kernel drops the flock with the process: no stale reclaim needed.
    with FileLock(path, owner="hermes-pull"):
        assert held_by(path)["owner"] == "hermes-pull"


def test_metadata_age_never_frees_a_live_lock(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    try:
        meta = path.with_name(path.name + ".meta.json")
        ancient = time.time() - 86400 * 30
        os.utime(meta, (ancient, ancient))
        with pytest.raises(LockBusy):
            FileLock(path, owner="hermes-pull").acquire()
    finally:
        holder.kill()
        holder.wait()


def test_positive_wait_raises_timeout_after_waiting(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    try:
        started = time.monotonic()
        with pytest.raises(LockTimeout):
            FileLock(path, owner="hermes-pull").acquire(wait_seconds=1)
        assert time.monotonic() - started >= 1
    finally:
        holder.kill()
        holder.wait()


def test_a_foreign_instance_cannot_release_the_lock(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    try:
        FileLock(path, owner="impostor").release()
        assert held_by(path) is not None
    finally:
        holder.kill()
        holder.wait()


def test_lock_file_survives_release(tmp_path):
    path = tmp_path / "network.lock"
    with FileLock(path, owner="hermes-pull"):
        pass
    assert path.exists()
    assert held_by(path) is None


def test_failed_metadata_write_does_not_leave_the_lock_held(tmp_path, monkeypatch):
    """A lock nobody can see is worse than no lock at all."""
    import hermes_backup.locks as module

    path = tmp_path / "network.lock"
    monkeypatch.setattr(
        module, "_meta_path", lambda p: tmp_path / "absent-dir" / "meta.json"
    )
    with pytest.raises(OSError):
        module.FileLock(path, owner="hermes-pull").acquire()
    monkeypatch.undo()
    assert held_by(path) is None


def test_double_acquire_on_one_instance_is_rejected(tmp_path):
    path = tmp_path / "network.lock"
    lock = FileLock(path, owner="hermes-pull")
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already held"):
            lock.acquire()
    finally:
        lock.release()


def test_metadata_records_pid_owner_and_time(tmp_path):
    path = tmp_path / "network.lock"
    with FileLock(path, owner="hermes-pull"):
        meta = json.loads(path.with_name(path.name + ".meta.json").read_text())
    assert meta["pid"] == os.getpid()
    assert meta["owner"] == "hermes-pull"
    assert meta["started_at"].endswith("Z")
