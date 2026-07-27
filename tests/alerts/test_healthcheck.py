from __future__ import annotations

import json
import subprocess

import pytest

from hermes_alerts.healthcheck import HealthcheckError, check


class Runner:
    def __init__(self, *, running=True, restarts=0, gateway_code=0):
        self.running = running
        self.restarts = restarts
        self.gateway_code = gateway_code

    def __call__(self, command):
        if command[:2] == ["docker", "inspect"] and "RestartCount" in command[3]:
            return subprocess.CompletedProcess(command, 0, f"{self.restarts}\n", "")
        if command[:2] == ["docker", "inspect"]:
            state = {
                "Running": self.running,
                "Status": "running" if self.running else "exited",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(state), "")
        return subprocess.CompletedProcess(command, self.gateway_code, "", "")


def test_healthy_container_and_gateway_pass(tmp_path):
    assert check(tmp_path, runner=Runner()) == "running restarts=0"


def test_stopped_container_fails(tmp_path):
    with pytest.raises(HealthcheckError, match="container_exited"):
        check(tmp_path, runner=Runner(running=False))


def test_gateway_failure_fails(tmp_path):
    with pytest.raises(HealthcheckError, match="gateway_status"):
        check(tmp_path, runner=Runner(gateway_code=1))


def test_restart_increase_fails_once_then_recovers(tmp_path):
    check(tmp_path, runner=Runner(restarts=0))
    with pytest.raises(HealthcheckError, match="restart_count_increased"):
        check(tmp_path, runner=Runner(restarts=1))
    assert check(tmp_path, runner=Runner(restarts=1)) == "running restarts=1"
