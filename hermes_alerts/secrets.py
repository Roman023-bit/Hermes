"""Read one credential from dotenv syntax without ever executing the file."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_ASSIGNMENT = re.compile(r"\A(?:export[ \t]+)?([A-Z][A-Z0-9_]*)=(.*)\Z")


class SecretError(ValueError):
    """The alert credential is missing or unsafe."""


def _parse_value(raw: str, key: str) -> str:
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        parts = list(lexer)
    except ValueError as error:
        raise SecretError(f"{key}: malformed quoted value") from error
    if len(parts) != 1 or not parts[0]:
        raise SecretError(f"{key}: empty or ambiguous value")
    return parts[0]


def read_token(path: Path, *, allow_primary_fallback: bool) -> str:
    keys = ["TELEGRAM_ALERT_BOT_TOKEN"]
    if allow_primary_fallback:
        keys.append("TELEGRAM_BOT_TOKEN")
    found: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise SecretError(f"{path}: unreadable credential file") from error
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(stripped)
        if not match or match.group(1) not in keys:
            continue
        key = match.group(1)
        if key in found:
            raise SecretError(f"{key}: duplicate assignment")
        found[key] = _parse_value(match.group(2), key)
    for key in keys:
        if key in found:
            return found[key]
    raise SecretError("Telegram alert credential is not configured")
