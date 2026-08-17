"""Fail-closed guard for updating a flattened upstream snapshot.

The local repository intentionally cannot merge upstream history.  This tool
records the complete local overlay before a new upstream tree is installed and
then proves that the overlay survived byte-for-byte in the staged result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

STATUS_OK = "hermes_upstream_guard_OK"
STATUS_BLOCKED = "hermes_upstream_guard_BLOCKED"
STATUS_FAILED = "hermes_upstream_guard_FAILED"


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


def _paths_between(repo: Path, old: str, new: str, *, cached: bool = False) -> set[str]:
    args = ["diff"]
    if cached:
        args.append("--cached")
    args.extend(["--name-only", "-z", old, new, "--"] if not cached else ["--name-only", "-z", old, "--"])
    raw = _git(repo, *args).stdout
    return {item.decode(errors="surrogateescape") for item in raw.split(b"\0") if item}


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


def prepare(repo: Path, baseline: str, target: str, output: Path) -> tuple[dict[str, object], bool]:
    _require_clean(repo)
    baseline_sha = _resolve(repo, baseline)
    current_sha = _resolve(repo, "HEAD")
    target_sha = _resolve(repo, target)
    overlay_paths = sorted(_paths_between(repo, baseline_sha, current_sha))
    upstream_paths = _paths_between(repo, baseline_sha, target_sha)
    collisions = sorted(set(overlay_paths) & upstream_paths)

    patch = _git(repo, "diff", "--binary", "--full-index", baseline_sha, current_sha, "--").stdout
    output.mkdir(parents=True, exist_ok=False)
    patch_path = output / "local-overlay.patch"
    patch_path.write_bytes(patch)
    manifest: dict[str, object] = {
        "format": 1,
        "baseline": baseline_sha,
        "current": current_sha,
        "target": target_sha,
        "overlay_paths": overlay_paths,
        "overlay_entries": {path: _tree_entry(repo, current_sha, path) for path in overlay_paths},
        "collisions": collisions,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest, not collisions


def verify(repo: Path, bundle: Path) -> None:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    patch = (bundle / "local-overlay.patch").read_bytes()
    if manifest.get("format") != 1:
        raise GuardError("unsupported manifest format")
    if manifest.get("collisions"):
        raise GuardError("manifest contains unresolved upstream/local collisions")
    if hashlib.sha256(patch).hexdigest() != manifest.get("patch_sha256"):
        raise GuardError("overlay patch digest does not match manifest")
    if _git(repo, "diff", "--quiet", check=False).returncode:
        raise GuardError("working tree has unstaged changes; verify the staged candidate only")

    target = _resolve(repo, str(manifest["target"]))
    expected_paths = set(manifest["overlay_paths"])
    actual_paths = _paths_between(repo, target, "HEAD", cached=True)
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise GuardError(f"overlay path mismatch; missing={missing}, unexpected={unexpected}")

    expected_entries = manifest["overlay_entries"]
    for path in sorted(expected_paths):
        if _index_entry(repo, path) != expected_entries[path]:
            raise GuardError(f"overlay content or mode changed: {path}")


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
                print(f"{STATUS_BLOCKED} collisions={manifest['collisions']}", file=sys.stderr)
                return 2
            print(f"{STATUS_OK} overlay_paths={len(manifest['overlay_paths'])} bundle={args.output}")
            return 0
        verify(args.repo, args.bundle)
        print(f"{STATUS_OK} verified_bundle={args.bundle}")
        return 0
    except (GuardError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"{STATUS_FAILED} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
