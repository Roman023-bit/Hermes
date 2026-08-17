from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_update.policy import POLICY_RELATIVE_PATH, PolicyError, load_policy
from hermes_update.snapshot_guard import main

WORKFLOW = ".github/workflows/ci.yml"

DEFAULT_POLICY = """
version: 1
rules:
  - strategy: upstream_wins
    reason: CI is a deliberate fork-wide divergence.
    paths:
      - ".github/workflows/*"
  - strategy: must_preserve
    paths:
      - "local.txt"
"""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def repository(
    tmp_path: Path,
    *,
    policy: str | None = DEFAULT_POLICY,
    overlay: dict[str, str] | None = None,
    upstream: dict[str, str] | None = None,
) -> tuple[Path, str, str, str]:
    """Build a baseline, a local overlay on top of it and a rival upstream tree.

    ``overlay`` and ``upstream`` add extra edits to their respective side, which
    is how a test declares a collision.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    write(repo / "upstream.txt", "base\n")
    write(repo / WORKFLOW, "upstream ci v1\n")
    write(repo / "stray.txt", "base\n")
    baseline = commit(repo, "baseline")
    git(repo, "tag", "baseline")

    write(repo / "local.txt", "local overlay\n")
    if policy is not None:
        write(repo / POLICY_RELATIVE_PATH, policy.lstrip())
    for name, text in (overlay or {}).items():
        write(repo / name, text)
    current = commit(repo, "local")
    git(repo, "tag", "current")

    git(repo, "switch", "-q", "--detach", baseline)
    write(repo / "upstream.txt", "new upstream\n")
    for name, text in (upstream or {}).items():
        write(repo / name, text)
    target = commit(repo, "target")
    git(repo, "tag", "target")
    git(repo, "switch", "-q", "--detach", current)
    return repo, baseline, current, target


def run_prepare(repo: Path, bundle: Path) -> int:
    return main(
        [
            "--repo",
            str(repo),
            "prepare",
            "--baseline",
            "baseline",
            "--target",
            "target",
            "--output",
            str(bundle),
        ]
    )


def run_verify(repo: Path, bundle: Path) -> int:
    return main(["--repo", str(repo), "verify", "--bundle", str(bundle)])


def stage_candidate(repo: Path, target: str, bundle: Path) -> None:
    git(repo, "restore", f"--source={target}", "--staged", "--worktree", "--", ".")
    patch = bundle / "local-overlay.patch"
    if patch.stat().st_size:
        git(repo, "apply", "--index", str(patch))


def manifest_of(bundle: Path) -> dict:
    return json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))


# --- existing snapshot workflow, unchanged behaviour -----------------------


def test_prepare_and_verify_complete_overlay(tmp_path: Path, capsys) -> None:
    repo, _, _, target = repository(tmp_path)
    bundle = tmp_path / "bundle"

    assert run_prepare(repo, bundle) == 0
    stage_candidate(repo, target, bundle)
    assert run_verify(repo, bundle) == 0
    assert "hermes_upstream_guard_OK" in capsys.readouterr().out


def test_prepare_accepts_a_precreated_empty_bundle_directory(tmp_path: Path) -> None:
    repo, _, _, _ = repository(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir(mode=0o700)

    assert run_prepare(repo, bundle) == 0
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "manifest.sha256").is_file()


def test_prepare_rejects_a_nonempty_bundle_directory(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    write(bundle / "keep.txt", "do not overwrite\n")

    assert run_prepare(repo, bundle) == 1
    assert "not empty" in capsys.readouterr().err
    assert (bundle / "keep.txt").read_text(encoding="utf-8") == "do not overwrite\n"


def test_verify_rejects_a_lost_overlay_file(tmp_path: Path, capsys) -> None:
    repo, _, _, target = repository(tmp_path)
    bundle = tmp_path / "bundle"
    assert run_prepare(repo, bundle) == 0
    stage_candidate(repo, target, bundle)
    git(repo, "restore", f"--source={target}", "--staged", "--worktree", "--", "local.txt")

    assert run_verify(repo, bundle) == 1
    assert "overlay path mismatch" in capsys.readouterr().err


def test_verify_rejects_a_tampered_patch(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path)
    bundle = tmp_path / "bundle"
    assert run_prepare(repo, bundle) == 0
    with (bundle / "local-overlay.patch").open("ab") as handle:
        handle.write(b"tampered\n")

    assert run_verify(repo, bundle) == 1
    assert "digest" in capsys.readouterr().err


def test_verify_rejects_a_tampered_manifest(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path)
    bundle = tmp_path / "bundle"
    assert run_prepare(repo, bundle) == 0
    manifest_path = bundle / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" \n")

    assert run_verify(repo, bundle) == 1
    assert "manifest digest" in capsys.readouterr().err


def test_verify_fails_closed_on_malformed_manifest(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    write(bundle / "manifest.json", "{}\n")
    write(bundle / "manifest.sha256", "not-a-valid-digest\n")
    write(bundle / "local-overlay.patch", "")

    assert run_verify(repo, bundle) == 1
    assert "hermes_upstream_guard_FAILED" in capsys.readouterr().err


# --- collisions -------------------------------------------------------------


def test_prepare_blocks_a_must_preserve_collision(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path, upstream={"local.txt": "upstream owns this now\n"})

    assert run_prepare(repo, tmp_path / "bundle") == 2
    err = capsys.readouterr().err
    assert "hermes_upstream_guard_BLOCKED" in err
    assert "local.txt — must_preserve" in err


def test_prepare_blocks_an_unclassified_collision(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(
        tmp_path,
        overlay={"stray.txt": "local edit\n"},
        upstream={"stray.txt": "upstream edit\n"},
    )

    assert run_prepare(repo, tmp_path / "bundle") == 2
    err = capsys.readouterr().err
    assert "stray.txt — unclassified" in err


def test_allowed_collision_prepares_and_is_reported(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(
        tmp_path,
        overlay={WORKFLOW: "local ci\n"},
        upstream={WORKFLOW: "upstream ci v2\n"},
    )
    bundle = tmp_path / "bundle"

    assert run_prepare(repo, bundle) == 0

    out = capsys.readouterr().out
    assert "hermes_upstream_guard_DISCARD" in out
    assert WORKFLOW in out
    assert "deliberate fork-wide divergence" in out
    assert "discarded=1" in out

    manifest = manifest_of(bundle)
    assert manifest["discarded_paths"] == [WORKFLOW]
    assert manifest["blocking"] == {}
    assert WORKFLOW not in manifest["overlay_paths"]
    assert manifest["discarded_entries"][WORKFLOW]["object"]
    # The surrendered path must not ride along in the patch, or applying it
    # would reinstate exactly the local version the policy just gave up.
    assert WORKFLOW.encode() not in (bundle / "local-overlay.patch").read_bytes()


def test_verify_accepts_a_candidate_whose_discard_took_upstream(tmp_path: Path) -> None:
    repo, _, _, target = repository(
        tmp_path,
        overlay={WORKFLOW: "local ci\n"},
        upstream={WORKFLOW: "upstream ci v2\n"},
    )
    bundle = tmp_path / "bundle"
    assert run_prepare(repo, bundle) == 0

    stage_candidate(repo, target, bundle)

    assert run_verify(repo, bundle) == 0
    assert (repo / WORKFLOW).read_text(encoding="utf-8") == "upstream ci v2\n"


def test_verify_rejects_a_discard_that_kept_the_local_version(tmp_path: Path, capsys) -> None:
    repo, _, current, target = repository(
        tmp_path,
        overlay={WORKFLOW: "local ci\n"},
        upstream={WORKFLOW: "upstream ci v2\n"},
    )
    bundle = tmp_path / "bundle"
    assert run_prepare(repo, bundle) == 0
    stage_candidate(repo, target, bundle)
    # Someone "helpfully" restored the local workflow after the import.
    git(repo, "restore", f"--source={current}", "--staged", "--worktree", "--", WORKFLOW)

    assert run_verify(repo, bundle) == 1
    assert WORKFLOW in capsys.readouterr().err


def test_blocked_run_still_reports_its_discards(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(
        tmp_path,
        overlay={WORKFLOW: "local ci\n"},
        upstream={WORKFLOW: "upstream ci v2\n", "local.txt": "upstream owns this now\n"},
    )

    assert run_prepare(repo, tmp_path / "bundle") == 2
    err = capsys.readouterr().err
    assert "hermes_upstream_guard_DISCARD" in err
    assert "hermes_upstream_guard_BLOCKED" in err


# --- policy validation, all fail-closed -------------------------------------


def test_missing_policy_fails_closed(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path, policy=None)

    assert run_prepare(repo, tmp_path / "bundle") == 1
    assert "overlay policy is missing" in capsys.readouterr().err


def test_corrupt_policy_fails_closed(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path, policy="version: 1\nrules: [oops\n")

    assert run_prepare(repo, tmp_path / "bundle") == 1
    assert "hermes_upstream_guard_FAILED" in capsys.readouterr().err


@pytest.mark.parametrize(
    "policy, expected",
    [
        ("version: 2\nrules: []\n", "unsupported overlay policy version"),
        ("version: 1\nrules: []\n", "non-empty 'rules' list"),
        ("[]\n", "must be a mapping"),
        (
            'version: 1\nrules:\n  - strategy: take_mine\n    paths: ["a.txt"]\n',
            "unknown strategy",
        ),
        (
            'version: 1\nrules:\n  - strategy: must_preserve\n    paths: []\n',
            "non-empty 'paths' list",
        ),
        (
            'version: 1\nrules:\n  - strategy: upstream_wins\n    paths: ["a.txt"]\n',
            "must state a reason",
        ),
        (
            'version: 1\nrules:\n  - strategy: must_preserve\n    paths: ["a.txt"]\n    nope: 1\n',
            "unknown keys",
        ),
    ],
)
def test_policy_structure_is_validated(tmp_path: Path, policy: str, expected: str) -> None:
    repo, _, _, _ = repository(tmp_path, policy=policy)

    with pytest.raises(PolicyError, match=expected):
        load_policy(repo)


@pytest.mark.parametrize("pattern", ["*", "**", "**/*.yml", "*/workflows/ci.yml", "*.yml/*"])
def test_overly_broad_patterns_are_rejected(tmp_path: Path, pattern: str) -> None:
    policy = f'version: 1\nrules:\n  - strategy: must_preserve\n    paths: ["{pattern}"]\n'
    repo, _, _, _ = repository(tmp_path, policy=policy)

    with pytest.raises(PolicyError):
        load_policy(repo)


def test_same_pattern_in_two_strategies_fails_closed(tmp_path: Path) -> None:
    policy = (
        "version: 1\n"
        "rules:\n"
        "  - strategy: must_preserve\n"
        '    paths: ["local.txt"]\n'
        "  - strategy: upstream_wins\n"
        "    reason: contradicts the rule above\n"
        '    paths: ["local.txt"]\n'
    )
    repo, _, _, _ = repository(tmp_path, policy=policy)

    with pytest.raises(PolicyError, match="claimed by both"):
        load_policy(repo)


def test_overlapping_patterns_fail_closed_at_classification(tmp_path: Path, capsys) -> None:
    # Two different patterns, both matching the same colliding path.
    policy = (
        "version: 1\n"
        "rules:\n"
        "  - strategy: upstream_wins\n"
        "    reason: CI diverges\n"
        '    paths: [".github/workflows/*"]\n'
        "  - strategy: must_preserve\n"
        f'    paths: ["{WORKFLOW}"]\n'
    )
    repo, _, _, _ = repository(
        tmp_path,
        policy=policy,
        overlay={WORKFLOW: "local ci\n"},
        upstream={WORKFLOW: "upstream ci v2\n"},
    )

    assert run_prepare(repo, tmp_path / "bundle") == 1
    assert "conflicting strategies" in capsys.readouterr().err


def test_verify_detects_a_policy_swap(tmp_path: Path, capsys) -> None:
    repo, _, _, target = repository(
        tmp_path,
        overlay={WORKFLOW: "local ci\n"},
        upstream={WORKFLOW: "upstream ci v2\n"},
    )
    bundle = tmp_path / "bundle"
    assert run_prepare(repo, bundle) == 0
    stage_candidate(repo, target, bundle)
    # Loosen the policy after the fact, then re-stage it.
    write(
        repo / POLICY_RELATIVE_PATH,
        "version: 1\n"
        "rules:\n"
        "  - strategy: upstream_wins\n"
        "    reason: quietly widened after the bundle was built\n"
        '    paths: [".github/workflows/*", "local.txt"]\n',
    )
    git(repo, "add", POLICY_RELATIVE_PATH)

    assert run_verify(repo, bundle) == 1
    assert "overlay policy changed" in capsys.readouterr().err
