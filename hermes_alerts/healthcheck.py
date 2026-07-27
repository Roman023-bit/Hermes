"""Host-side Hermes container and gateway healthcheck."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from hermes_backup.hashing import atomic_write_text
from hermes_backup.status import status_line, write_status


class HealthcheckError(RuntimeError):
    """The production container or gateway is not healthy."""


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def check(state_root: Path, *, runner=_run) -> str:
    inspect = runner([
        "docker",
        "inspect",
        "--format",
        "{{json .State}}",
        "hermes",
    ])
    if inspect.returncode:
        raise HealthcheckError("container_missing")
    try:
        state = json.loads(inspect.stdout)
    except json.JSONDecodeError as error:
        raise HealthcheckError("docker_state_unparsable") from error
    if state.get("Running") is not True or state.get("Status") != "running":
        raise HealthcheckError(f"container_{state.get('Status', 'unknown')}")

    restart_result = runner([
        "docker",
        "inspect",
        "--format",
        "{{.RestartCount}}",
        "hermes",
    ])
    try:
        restarts = int(restart_result.stdout.strip())
    except ValueError as error:
        raise HealthcheckError("restart_count_unparsable") from error
    if restart_result.returncode or restarts < 0:
        raise HealthcheckError("restart_count_unavailable")

    state_root.mkdir(parents=True, exist_ok=True)
    state_root.chmod(0o700)
    count_path = state_root / "hermes-restart-count"
    try:
        previous = int(count_path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        previous = restarts
    except (OSError, ValueError) as error:
        raise HealthcheckError("restart_state_unreadable") from error
    atomic_write_text(count_path, f"{restarts}\n")
    if restarts > previous:
        raise HealthcheckError(f"restart_count_increased {previous}->{restarts}")

    gateway = runner(["docker", "exec", "hermes", "hermes", "gateway", "status"])
    if gateway.returncode:
        raise HealthcheckError(f"gateway_status_exit={gateway.returncode}")
    return f"running restarts={restarts}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--status-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = check(args.state_root)
    except (HealthcheckError, OSError) as error:
        write_status(args.status_dir, "hermes_healthcheck", "FAILED", reason=str(error))
        print(status_line("healthcheck", "FAILED", str(error)), file=sys.stderr)
        return 1
    write_status(args.status_dir, "hermes_healthcheck", "OK")
    print(status_line("healthcheck", "OK", result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
