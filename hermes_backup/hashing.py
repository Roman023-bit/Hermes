"""Checksums and atomic writes shared by every backup component."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_BLOCK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_sha256sums(directory: Path, exclude: str = "SHA256SUMS") -> Path:
    # A checksum file cannot contain its own checksum, so it is skipped.
    lines = [
        f"{sha256_file(item)}  {item.name}\n"
        for item in sorted(directory.iterdir())
        if item.is_file() and item.name != exclude and not item.name.startswith(".")
    ]
    target = directory / exclude
    atomic_write_text(target, "".join(lines))
    return target
