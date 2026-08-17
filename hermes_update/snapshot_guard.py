"""Fail-closed guard for updating a flattened upstream snapshot.

The local repository intentionally cannot merge upstream history.  This tool
records the complete local overlay before a new upstream tree is installed and
then proves that the overlay survived byte-for-byte in the staged result.

Paths that upstream and the overlay both changed are collisions.  They are
classified by ``deploy/beget/upstream-policy.yaml``: ``must_preserve`` and
unclassified collisions block the import, while ``upstream_wins`` collisions are
accepted and their local change is discarded — never silently, always listed in
the report and recorded in the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from hermes_update.policy import (
    MUST_PRESERVE,
    POLICY_RELATIVE_PATH,
    UNCLASSIFIED,
    UPSTREAM_WINS,
    Policy,
    PolicyError,
    load_policy,
)

STATUS_OK = "hermes_upstream_guard_OK"
STATUS_BLOCKED = "hermes_upstream_guard_BLOCKED"
STATUS_FAILED = "hermes_upstream_guard_FAILED"
STATUS_DISCARD = "hermes_upstream_guard_DISCARD"

MANIFEST_FORMAT = 2
MANIFEST_NAME = "manifest.json"
MANIFEST_DIGEST_NAME = "manifest.sha256"


class GuardError(RuntimeError):
    """An update invariant was not satisfied."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise GuardError(f"git {' '.join(args)} failed: {detail}")
    return result


def _resolve(repo: Path, revision: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.decode().strip()


def _decode_paths(raw: bytes) -> set[str]:
    return {item.decode(errors="surrogateescape") for item in raw.split(b"\0") if item}


def _paths_between(repo: Path, old: str, new: str) -> set[str]:
    return _decode_paths(_git(repo, "diff", "--name-only", "-z", old, new, "--").stdout)


def _paths_staged_against(repo: Path, old: str) -> set[str]:
    return _decode_paths(_git(repo, "diff", "--cached", "--name-only", "-z", old, "--").stdout)


def _tree_entry(repo: Path, revision: str, path: str) -> dict[str, str] | None:
    raw = _git(repo, "ls-tree", "-z", revision, "--", path).stdout
    if not raw:
        return None
    metadata, listed_path = raw[:-1].split(b"\t", 1)
    mode, object_type, object_id = metadata.decode().split()
    if listed_path.decode(errors="surrogateescape") != path or object_type != "blob":
        raise GuardError(f"unsupported non-file overlay path: {path}")
    return {"mode": mode, "object": object_id}


def _index_entry(repo: Path, path: str) -> dict[str, str] | None:
    raw = _git(repo, "ls-files", "--stage", "-z", "--", path).stdout
    if not raw:
        return None
    metadata, listed_path = raw[:-1].split(b"\t", 1)
    mode, object_id, stage = metadata.decode().split()
    if listed_path.decode(errors="surrogateescape") != path or stage != "0":
        raise GuardError(f"unmerged or ambiguous index entry: {path}")
    return {"mode": mode, "object": object_id}


def _require_clean(repo: Path) -> None:
    if _git(repo, "status", "--porcelain", "--untracked-files=all").stdout:
        raise GuardError("working tree is not clean")


def _prepare_output_directory(output: Path) -> None:
    """Create *output*, or accept a securely pre-created empty directory."""
    if output.is_symlink():
        raise GuardError(f"bundle output must not be a symlink: {output}")
    if output.exists():
        if not output.is_dir():
            raise GuardError(f"bundle output is not a directory: {output}")
        if any(output.iterdir()):
            raise GuardError(f"bundle output directory is not empty: {output}")
        return
    output.mkdir(parents=True)


def _classify(policy: Policy, collisions: list[str]) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Split collisions into discarded and blocking, keeping the reason for each."""
    discarded: list[str] = []
    blocking: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for path in collisions:
        strategy, reason = policy.classify(path)
        if strategy == UPSTREAM_WINS:
            discarded.append(path)
            reasons[path] = reason
        else:
            # MUST_PRESERVE and UNCLASSIFIED both stop the import; the label is
            # kept so the operator knows whether a rule spoke or nobody did.
            blocking[path] = MUST_PRESERVE if strategy == MUST_PRESERVE else UNCLASSIFIED
    return discarded, blocking, reasons


def prepare(
    repo: Path, baseline: str, target: str, output: Path
) -> tuple[dict[str, object], bool]:
    _require_clean(repo)
    policy = load_policy(repo)
    baseline_sha = _resolve(repo, baseline)
    current_sha = _resolve(repo, "HEAD")
    target_sha = _resolve(repo, target)
    overlay_all = sorted(_paths_between(repo, baseline_sha, current_sha))
    upstream_paths = _paths_between(repo, baseline_sha, target_sha)
    collisions = sorted(set(overlay_all) & upstream_paths)

    discarded, blocking, reasons = _classify(policy, collisions)
    # The discarded paths must not travel in the patch: re-applying them would
    # reinstate exactly the local version the policy just surrendered.
    discarded_set = set(discarded)
    overlay_paths = [path for path in overlay_all if path not in discarded_set]

    if overlay_paths:
        patch = _git(
            repo,
            "diff",
            "--binary",
            "--full-index",
            baseline_sha,
            current_sha,
            "--",
            *overlay_paths,
        ).stdout
    else:
        # An empty pathspec means "everything" to git, so never send one.
        patch = b""

    _prepare_output_directory(output)
    patch_path = output / "local-overlay.patch"
    patch_path.write_bytes(patch)
    manifest: dict[str, object] = {
        "format": MANIFEST_FORMAT,
        "baseline": baseline_sha,
        "current": current_sha,
        "target": target_sha,
        "policy_path": policy.source,
        "policy_sha256": policy.digest,
        "overlay_paths": overlay_paths,
        "overlay_entries": {path: _tree_entry(repo, current_sha, path) for path in overlay_paths},
        "discarded_paths": discarded,
        "discarded_entries": {path: _tree_entry(repo, current_sha, path) for path in discarded},
        "discarded_reasons": reasons,
        "blocking": blocking,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (output / MANIFEST_NAME).write_bytes(manifest_bytes)
    (output / MANIFEST_DIGEST_NAME).write_text(
        hashlib.sha256(manifest_bytes).hexdigest() + "\n", encoding="ascii"
    )
    return manifest, not blocking


def verify(repo: Path, bundle: Path) -> None:
    manifest_bytes = (bundle / MANIFEST_NAME).read_bytes()
    expected_manifest_digest = (bundle / MANIFEST_DIGEST_NAME).read_text(
        encoding="ascii"
    ).strip()
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_digest:
        raise GuardError("manifest digest does not match bundle")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    patch = (bundle / "local-overlay.patch").read_bytes()
    if manifest.get("format") != MANIFEST_FORMAT:
        raise GuardError("unsupported manifest format")
    if manifest.get("blocking"):
        raise GuardError("manifest contains unresolved upstream/local collisions")
    if hashlib.sha256(patch).hexdigest() != manifest.get("patch_sha256"):
        raise GuardError("overlay patch digest does not match manifest")
    if _git(repo, "diff", "--quiet", check=False).returncode:
        raise GuardError("working tree has unstaged changes; verify the staged candidate only")

    # The policy governs which local changes were dropped, so a swapped policy
    # invalidates the whole decision. Re-load it too: a corrupt policy in the
    # candidate tree must fail here rather than at the next import.
    policy = load_policy(repo, str(manifest.get("policy_path", POLICY_RELATIVE_PATH)))
    if policy.digest != manifest.get("policy_sha256"):
        raise GuardError("overlay policy changed since the bundle was prepared")

    target = _resolve(repo, str(manifest["target"]))
    expected_paths = set(manifest["overlay_paths"])
    actual_paths = _paths_staged_against(repo, target)
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise GuardError(f"overlay path mismatch; missing={missing}, unexpected={unexpected}")

    expected_entries = manifest["overlay_entries"]
    for path in sorted(expected_paths):
        if _index_entry(repo, path) != expected_entries[path]:
            raise GuardError(f"overlay content or mode changed: {path}")

    # A discarded path must actually hold upstream's version now. Path-set
    # equality above already implies it, but an explicit check names the file
    # instead of reporting it as an unexpected extra.
    for path in manifest.get("discarded_paths", []):
        if _index_entry(repo, path) != _tree_entry(repo, target, path):
            raise GuardError(f"discarded path does not match upstream: {path}")


def _report_discards(manifest: dict[str, object], stream) -> None:
    discarded = list(manifest.get("discarded_paths", []))  # type: ignore[arg-type]
    if not discarded:
        return
    reasons: dict[str, str] = manifest.get("discarded_reasons", {})  # type: ignore[assignment]
    print(
        f"{STATUS_DISCARD} {len(discarded)} local change(s) will be dropped in favour of upstream:",
        file=stream,
    )
    for path in discarded:
        print(f"{STATUS_DISCARD}   {path} — {reasons.get(path, '')}", file=stream)


def _report_blocking(manifest: dict[str, object], stream) -> None:
    blocking: dict[str, str] = manifest.get("blocking", {})  # type: ignore[assignment]
    print(f"{STATUS_BLOCKED} {len(blocking)} collision(s) must be resolved by hand:", file=stream)
    for path in sorted(blocking):
        print(f"{STATUS_BLOCKED}   {path} — {blocking[path]}", file=stream)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--baseline", required=True)
    prepare_parser.add_argument("--target", required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--bundle", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            manifest, safe = prepare(args.repo, args.baseline, args.target, args.output)
            if not safe:
                # Discards are reported even on a blocked run: the operator
                # resolving the blockers needs the whole picture, not half.
                _report_discards(manifest, sys.stderr)
                _report_blocking(manifest, sys.stderr)
                return 2
            _report_discards(manifest, sys.stdout)
            print(
                f"{STATUS_OK} overlay_paths={len(manifest['overlay_paths'])} "  # type: ignore[arg-type]
                f"discarded={len(manifest['discarded_paths'])} "  # type: ignore[arg-type]
                f"bundle={args.output}"
            )
            return 0
        verify(args.repo, args.bundle)
        print(f"{STATUS_OK} verified_bundle={args.bundle}")
        return 0
    except (
        GuardError,
        PolicyError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"{STATUS_FAILED} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
