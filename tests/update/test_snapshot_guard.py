from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_update.snapshot_guard import main


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


def repository(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    write(repo / "upstream.txt", "base\n")
    baseline = commit(repo, "baseline")
    git(repo, "tag", "baseline")

    write(repo / "local.txt", "local overlay\n")
    current = commit(repo, "local")
    git(repo, "tag", "current")

    git(repo, "switch", "-q", "--detach", baseline)
    write(repo / "upstream.txt", "new upstream\n")
    target = commit(repo, "target")
    git(repo, "tag", "target")
    git(repo, "switch", "-q", "--detach", current)
    return repo, baseline, current, target


def stage_candidate(repo: Path, target: str, bundle: Path) -> None:
    git(repo, "restore", f"--source={target}", "--staged", "--worktree", "--", ".")
    git(repo, "apply", "--index", str(bundle / "local-overlay.patch"))


def test_prepare_and_verify_complete_overlay(tmp_path: Path, capsys) -> None:
    repo, _, _, target = repository(tmp_path)
    bundle = tmp_path / "bundle"

    assert main(["--repo", str(repo), "prepare", "--baseline", "baseline", "--target", "target", "--output", str(bundle)]) == 0
    stage_candidate(repo, target, bundle)
    assert main(["--repo", str(repo), "verify", "--bundle", str(bundle)]) == 0
    assert "hermes_upstream_guard_OK" in capsys.readouterr().out


def test_prepare_blocks_colliding_changes(tmp_path: Path, capsys) -> None:
    repo, baseline, _, _ = repository(tmp_path)
    git(repo, "switch", "-q", "--detach", baseline)
    write(repo / "local.txt", "upstream also owns this path\n")
    target = commit(repo, "colliding target")
    git(repo, "tag", "-f", "target", target)
    git(repo, "switch", "-q", "--detach", "current")

    result = main(["--repo", str(repo), "prepare", "--baseline", "baseline", "--target", "target", "--output", str(tmp_path / "bundle")])

    assert result == 2
    assert "hermes_upstream_guard_BLOCKED" in capsys.readouterr().err


def test_verify_rejects_a_lost_overlay_file(tmp_path: Path, capsys) -> None:
    repo, _, _, target = repository(tmp_path)
    bundle = tmp_path / "bundle"
    assert main(["--repo", str(repo), "prepare", "--baseline", "baseline", "--target", "target", "--output", str(bundle)]) == 0
    stage_candidate(repo, target, bundle)
    git(repo, "restore", f"--source={target}", "--staged", "--worktree", "--", "local.txt")

    assert main(["--repo", str(repo), "verify", "--bundle", str(bundle)]) == 1
    assert "overlay path mismatch" in capsys.readouterr().err


def test_verify_rejects_a_tampered_patch(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path)
    bundle = tmp_path / "bundle"
    assert main(["--repo", str(repo), "prepare", "--baseline", "baseline", "--target", "target", "--output", str(bundle)]) == 0
    with (bundle / "local-overlay.patch").open("ab") as handle:
        handle.write(b"tampered\n")

    assert main(["--repo", str(repo), "verify", "--bundle", str(bundle)]) == 1
    assert "digest" in capsys.readouterr().err


def test_verify_fails_closed_on_malformed_manifest(tmp_path: Path, capsys) -> None:
    repo, _, _, _ = repository(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    write(bundle / "manifest.json", "{}\n")
    write(bundle / "local-overlay.patch", "")

    assert main(["--repo", str(repo), "verify", "--bundle", str(bundle)]) == 1
    assert "hermes_upstream_guard_FAILED" in capsys.readouterr().err
