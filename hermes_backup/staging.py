"""Copy a live tree into staging and prove the copy is stable.

The backup lock stops other backups, not Hermes: sessions and cron state
can change mid-copy. Copy, then re-check with a checksum dry run, repeat
while anything moved, and fail closed rather than publish a torn file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from hermes_backup.inventory import EXCLUDE_RULES

# An itemized line is eleven flag characters and a path: the first says how
# the entry changed, the second what kind of entry it is. rsync also writes
# notes like `skipping non-regular file "pipe"` to stdout, and counting those
# as churn would fail every single attempt for as long as a FIFO exists —
# and no retry can ever clear it.
_ITEMIZED = re.compile(r"\A(\*deleting|[<>ch.][fdLDS])")

# -rlptgoH is -a without -D: ownership and hardlinks are preserved, while
# device nodes and FIFOs are left behind — hashing a FIFO would hang the
# backup instead of failing it.
RSYNC_FLAGS = (
    "-rlptgoH",
    "--numeric-ids",
    "--delete",
    "--delete-excluded",
    "--itemize-changes",
)
VANISHED_EXIT = 24


class UnstableSourceError(RuntimeError):
    """The source kept changing, so no consistent staging copy exists."""


class VanishedFiles(RuntimeError):
    """rsync exit 24: files disappeared mid-transfer. Retryable churn."""


def rsync_filter(rule: str) -> str:
    """Translate a root-relative fnmatch rule into an rsync filter.

    rsync matches an unanchored pattern against the END of a path, so a
    bare `cache/*` would also delete workspace/project/cache. Python's
    rules are root-relative, so every rooted rule gains a leading slash.
    """
    if rule.startswith("**/"):
        return rule[:-1] if rule.endswith("/*") else rule
    if rule.startswith("*/"):
        return f"**/{rule[2:]}"
    if rule.endswith("/*"):
        return f"/{rule[:-1]}"
    return f"/{rule}"


def rsync_command(
    source: Path, staging: Path, *, dry_run: bool, rsync: str = "rsync"
) -> list[str]:
    command = [rsync, *RSYNC_FLAGS]
    command += [f"--exclude={rsync_filter(rule)}" for rule in EXCLUDE_RULES]
    if dry_run:
        command += ["--dry-run", "--checksum"]
    command += [f"{source}/", f"{staging}/"]
    return command


def _run_rsync(source: Path, staging: Path, dry_run: bool, rsync: str) -> str:
    result = subprocess.run(
        rsync_command(source, staging, dry_run=dry_run, rsync=rsync),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == VANISHED_EXIT:
        raise VanishedFiles(f"rsync exit {VANISHED_EXIT}: source files vanished")
    if result.returncode != 0:
        raise UnstableSourceError(
            f"rsync failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def changed_paths(output: str) -> list[str]:
    """Itemized changes only, dropping rsync's informational chatter."""
    return [line for line in output.splitlines() if _ITEMIZED.match(line)]


def stabilized_copy(
    source: Path, staging: Path, attempts: int = 4, rsync: str = "rsync"
) -> int:
    if not source.is_dir():
        raise UnstableSourceError(f"source is not a directory: {source}")
    staging.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            _run_rsync(source, staging, False, rsync)
            changed = changed_paths(_run_rsync(source, staging, True, rsync))
        except VanishedFiles:
            # The tree moved under us: that is exactly what the retry is for.
            continue
        if not changed:
            return attempt
    raise UnstableSourceError(
        f"unstable_source: {len(changed)} path(s) still changing after {attempts} attempts"
    )
