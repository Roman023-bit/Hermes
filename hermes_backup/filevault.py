"""Runtime gate: never write secrets onto an unencrypted disk.

`fdesetup isactive` prints true/false and exits accordingly, so the gate
reads an exit code instead of parsing human-readable status output.
"""

from __future__ import annotations

import subprocess


class FileVaultOff(RuntimeError):
    """FileVault is not active, so off-site secrets must not be written."""


def require_filevault(command: list[str] | None = None) -> None:
    argv = command or ["/usr/bin/fdesetup", "isactive"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as error:
        raise FileVaultOff(f"cannot run {argv[0]}: {error}") from error
    if result.returncode != 0:
        raise FileVaultOff("filevault_off")
