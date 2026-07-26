"""STATE: expectations a restore drill checks a backup against.

The file lives inside the backup directory, so it is untrusted input: it
is parsed with a key whitelist and never sourced by a shell.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

INT_KEYS = frozenset({
    "BACKUP_FORMAT_VERSION",
    "STATE_DB_PAGE_COUNT",
    "STATE_DB_USER_VERSION",
    "KANBAN_DB_PAGE_COUNT",
    "KANBAN_DB_USER_VERSION",
    "EXPECTED_SESSIONS",
    "EXPECTED_SKILLS",
    "EXPECTED_PLUGINS",
    "EXPECTED_CRON_JOBS",
    "ESSENTIAL_FILE_COUNT",
    "ESSENTIAL_TOTAL_BYTES",
    "UNCLASSIFIED_FILE_COUNT",
    "EXCLUDED_SPECIAL_COUNT",
    "EXCLUDED_ESCAPING_LINK_COUNT",
})
STR_KEYS = frozenset({
    "CREATED_AT",
    "SOURCE_HOST",
    "HERMES_GIT_SHA",
    "HERMES_IMAGE_ID",
    "HERMES_IMAGE_REF",
    "STATE_DB_SHA256",
    "KANBAN_DB_SHA256",
})
ALL_KEYS = INT_KEYS | STR_KEYS
_SAFE_STR = re.compile(r"\A[A-Za-z0-9:._@/+-]{1,200}\Z")


class StateError(ValueError):
    """STATE is malformed, incomplete, or carries an unexpected key."""


def format_state(values: Mapping[str, int | str]) -> str:
    missing = ALL_KEYS - set(values)
    if missing:
        raise StateError(f"missing key: {sorted(missing)[0]}")
    unknown = set(values) - ALL_KEYS
    if unknown:
        raise StateError(f"unknown key: {sorted(unknown)[0]}")
    for key in sorted(STR_KEYS):
        # Values arrive from `docker inspect` and `git`: reject a bad one
        # where it is produced, not three steps later in the self-check.
        # str() is deliberately not applied — a number here means the
        # caller mixed up its keys, and coercion would hide that.
        value = values[key]
        if not isinstance(value, str) or not _SAFE_STR.match(value):
            raise StateError(f"{key} has an unsafe value")
    for key in sorted(INT_KEYS):
        if isinstance(values[key], bool) or not isinstance(values[key], int):
            raise StateError(f"{key} expects an integer")
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def parse_state(text: str) -> dict[str, int | str]:
    parsed: dict[str, int | str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise StateError(f"line {number}: expected KEY=VALUE")
        if key not in ALL_KEYS:
            raise StateError(f"line {number}: unknown key {key!r}")
        if key in parsed:
            raise StateError(f"line {number}: duplicate key {key!r}")
        if key in INT_KEYS:
            if not re.fullmatch(r"-?[0-9]+", value):
                raise StateError(f"line {number}: {key} expects an integer")
            parsed[key] = int(value)
        elif not _SAFE_STR.match(value):
            raise StateError(f"line {number}: {key} has an unsafe value")
        else:
            parsed[key] = value
    missing = ALL_KEYS - set(parsed)
    if missing:
        raise StateError(f"missing key: {sorted(missing)[0]}")
    return parsed
