"""Overlay policy for the flattened-snapshot update guard.

A collision is a path that both upstream and the local overlay changed since
the recorded baseline.  Without classification every release collides on the CI
workflows, so the guard is permanently red and stops being read.  This module
turns collisions into two explicit, versioned decisions:

``upstream_wins``
    The collision is expected.  The local change on that path is DISCARDED at
    import — excluded from the overlay patch, reported, and recorded in the
    manifest so the loss is never silent.

``must_preserve``
    The collision is fatal.  The import stops until an operator resolves it.

Anything else — an unmatched path, a malformed file, an unknown strategy, a
path claimed by both strategies — fails closed and blocks the import.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

POLICY_RELATIVE_PATH = "deploy/beget/upstream-policy.yaml"

UPSTREAM_WINS = "upstream_wins"
MUST_PRESERVE = "must_preserve"
STRATEGIES = (UPSTREAM_WINS, MUST_PRESERVE)

UNCLASSIFIED = "unclassified"

_SUPPORTED_VERSION = 1


class PolicyError(ValueError):
    """The overlay policy is missing, malformed or ambiguous."""


def _require_pattern(pattern: object, strategy: str) -> str:
    """Reject patterns that cannot be reasoned about at review time.

    A pattern is a per-component glob: ``*`` never crosses a ``/``.  The first
    component must be literal, so a rule always names the directory it governs,
    and at least one component must be literal, so no rule can quietly claim the
    whole tree.
    """
    if not isinstance(pattern, str) or not pattern:
        raise PolicyError(f"{strategy}: path patterns must be non-empty strings")
    if pattern != pattern.strip():
        raise PolicyError(f"{strategy}: path pattern has surrounding whitespace: {pattern!r}")
    if "**" in pattern:
        raise PolicyError(f"{strategy}: recursive '**' patterns are not allowed: {pattern!r}")
    if pattern.startswith("/") or pattern.endswith("/"):
        raise PolicyError(f"{strategy}: path pattern must be repo-relative: {pattern!r}")
    if "\\" in pattern:
        raise PolicyError(f"{strategy}: path pattern must use '/' separators: {pattern!r}")

    components = pattern.split("/")
    if any(not component for component in components):
        raise PolicyError(f"{strategy}: path pattern has an empty component: {pattern!r}")
    if any(component in {".", ".."} for component in components):
        raise PolicyError(f"{strategy}: path pattern must not traverse: {pattern!r}")

    literal = [c for c in components if not _is_wildcard(c)]
    if not literal:
        raise PolicyError(f"{strategy}: path pattern is too broad: {pattern!r}")
    if _is_wildcard(components[0]):
        raise PolicyError(
            f"{strategy}: path pattern must start with a literal component: {pattern!r}"
        )
    return pattern


def _is_wildcard(component: str) -> bool:
    return any(character in component for character in "*?[")


def _matches(path: str, pattern: str) -> bool:
    """Match component-by-component so ``*`` cannot swallow a directory."""
    path_components = path.split("/")
    pattern_components = pattern.split("/")
    if len(path_components) != len(pattern_components):
        return False
    return all(
        fnmatch.fnmatchcase(actual, expected)
        for actual, expected in zip(path_components, pattern_components)
    )


@dataclass(frozen=True)
class Policy:
    """An immutable, digest-identified set of collision rules."""

    rules: tuple[tuple[str, str, str], ...]  # (pattern, strategy, reason)
    digest: str
    source: str

    def classify(self, path: str) -> tuple[str, str]:
        """Return ``(strategy, reason)``; ``UNCLASSIFIED`` when no rule matches.

        A path claimed by both strategies is ambiguous and fails closed rather
        than resolving by rule order — order-dependent security decisions are a
        bug waiting for a reviewer to misread the file.
        """
        matched: dict[str, str] = {}
        patterns: dict[str, str] = {}
        for pattern, strategy, reason in self.rules:
            if _matches(path, pattern):
                matched.setdefault(strategy, reason)
                patterns.setdefault(strategy, pattern)
        if not matched:
            return UNCLASSIFIED, ""
        if len(matched) > 1:
            claims = ", ".join(f"{strategy} via {patterns[strategy]!r}" for strategy in sorted(matched))
            raise PolicyError(f"path claimed by conflicting strategies: {path} ({claims})")
        strategy, reason = next(iter(matched.items()))
        return strategy, reason


def load_policy(repo: Path, relative_path: str = POLICY_RELATIVE_PATH) -> Policy:
    """Read and fully validate the policy, or raise ``PolicyError``."""
    path = repo / relative_path
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as error:
        raise PolicyError(f"overlay policy is missing: {relative_path}") from error
    except OSError as error:
        raise PolicyError(f"overlay policy is unreadable: {relative_path}: {error}") from error

    try:
        document = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise PolicyError(f"overlay policy is not valid YAML: {relative_path}: {error}") from error

    if not isinstance(document, dict):
        raise PolicyError(f"overlay policy must be a mapping: {relative_path}")
    if document.get("version") != _SUPPORTED_VERSION:
        raise PolicyError(f"unsupported overlay policy version: {document.get('version')!r}")

    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise PolicyError("overlay policy must declare a non-empty 'rules' list")

    rules: list[tuple[str, str, str]] = []
    owners: dict[str, str] = {}
    for index, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            raise PolicyError(f"rule {index} must be a mapping")
        unknown = set(rule) - {"strategy", "paths", "reason"}
        if unknown:
            raise PolicyError(f"rule {index} has unknown keys: {sorted(unknown)}")

        strategy = rule.get("strategy")
        if strategy not in STRATEGIES:
            raise PolicyError(f"rule {index} has unknown strategy: {strategy!r}")

        reason = rule.get("reason", "")
        if not isinstance(reason, str):
            raise PolicyError(f"rule {index} has a non-string reason")
        reason = " ".join(reason.split())
        # Discarding local work needs a written justification; preserving does
        # not, because preserving is the safe default.
        if strategy == UPSTREAM_WINS and not reason:
            raise PolicyError(f"rule {index} ({UPSTREAM_WINS}) must state a reason")

        paths = rule.get("paths")
        if not isinstance(paths, list) or not paths:
            raise PolicyError(f"rule {index} must declare a non-empty 'paths' list")
        for pattern in paths:
            pattern = _require_pattern(pattern, str(strategy))
            previous = owners.get(pattern)
            if previous is not None and previous != strategy:
                raise PolicyError(
                    f"pattern {pattern!r} is claimed by both {previous} and {strategy}"
                )
            owners[pattern] = str(strategy)
            rules.append((pattern, str(strategy), reason))

    return Policy(
        rules=tuple(rules),
        digest=hashlib.sha256(raw_bytes).hexdigest(),
        source=relative_path,
    )
