"""Archive creation and paranoid extraction.

The drill unpacks an archive that travelled over the network, so every
member is checked before a single byte is written: absolute paths,
traversal, links pointing outside the tree, and any special object.
"""

from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath

_ALLOWED_TYPES = {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}


class ArchiveError(RuntimeError):
    """An archive member is unsafe to extract."""


def _check_member(member: tarfile.TarInfo) -> None:
    name = PurePosixPath(member.name)
    if name.is_absolute() or member.name.startswith("/"):
        raise ArchiveError(f"{member.name}: absolute path")
    if ".." in name.parts:
        raise ArchiveError(f"{member.name}: parent traversal")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute() or ".." in target.parts:
            raise ArchiveError(f"{member.name}: link escapes the archive")
        return
    if member.type not in _ALLOWED_TYPES:
        raise ArchiveError(f"{member.name}: special object of type {member.type!r}")


def validate(tar_path: Path) -> None:
    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar:
                _check_member(member)
    except tarfile.TarError as error:
        raise ArchiveError(f"{tar_path}: {error}") from error


def create(staging: Path, dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        for path in sorted(staging.rglob("*")):
            tar.add(path, arcname=path.relative_to(staging).as_posix(), recursive=False)
    dest.chmod(0o600)


def extract(tar_path: Path, dest: Path) -> None:
    validate(tar_path)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:*") as tar:
        # filter="tar" keeps modes and internal symlinks, which the backup
        # needs; safety comes from validate() above, not from the filter.
        # Naming it also pins behaviour across the 3.14 default change.
        tar.extractall(dest, filter="tar")  # noqa: S202 — every member validated above
