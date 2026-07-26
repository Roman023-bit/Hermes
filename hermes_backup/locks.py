"""One narrow uplink, two pullers: whoever holds the lock transfers.

flock is used rather than a lock directory because the kernel releases it
when the holder dies. A directory protocol has to decide whether a lock
is stale, and every such decision races: between mkdir and writing the
metadata a new lock looks abandoned, and between reading a dead pid and
removing the directory a third process can take it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_POLL_SECONDS = 5
_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


class LockBusy(RuntimeError):
    """Another process holds the lock and no waiting was requested."""


class LockTimeout(RuntimeError):
    """The lock stayed held for the whole allowed wait."""


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def read_meta(path: Path) -> dict | None:
    try:
        return json.loads(_meta_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def held_by(path: Path) -> dict | None:
    """Metadata of the current holder, or None when the lock is free."""
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return read_meta(path) or {}
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


class FileLock:
    def __init__(self, path: Path, owner: str) -> None:
        self.path = path
        self.owner = owner
        self._fd: int | None = None

    def acquire(self, wait_seconds: int = 0) -> "FileLock":
        if self._fd is not None:
            raise RuntimeError("lock already held by this instance")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The file itself is permanent; only the flock on it comes and goes.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        # O_CREAT's mode only applies when the file is new, and this one
        # outlives every run: fix the mode on an inherited file too.
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                # Only contention is retryable. EBADF, ENOLCK and friends
                # mean something is wrong with the file, not that someone
                # else holds it, and must not masquerade as a busy lock.
                if error.errno not in _CONTENDED:
                    os.close(fd)
                    raise
                holder = read_meta(self.path) or {}
                if wait_seconds <= 0:
                    os.close(fd)
                    raise LockBusy(
                        f"held by {holder.get('owner', 'unknown')}"
                    ) from None
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockTimeout(
                        f"still held by {holder.get('owner', 'unknown')} after {wait_seconds}s"
                    ) from None
                time.sleep(min(_POLL_SECONDS, max(0.1, deadline - time.monotonic())))
        self._fd = fd
        try:
            self._write_meta()
        except OSError:
            # Never return holding a lock the caller does not know about.
            self.release()
            raise
        return self

    def _write_meta(self) -> None:
        meta = _meta_path(self.path)
        tmp = meta.with_name(f".{meta.name}.tmp")
        tmp.write_text(
            json.dumps({
                "pid": os.getpid(),
                "owner": self.owner,
                "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, meta)

    def release(self) -> None:
        # Only ever release a descriptor this instance owns: another object
        # pointing at the same path must not be able to free someone else.
        if self._fd is None:
            return
        _meta_path(self.path).unlink(missing_ok=True)  # missing after a failed write
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
