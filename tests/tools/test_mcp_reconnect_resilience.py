"""Reconnect resilience: infinite reconnect with reset-on-success.

Root cause (2026-07-18): MCPServerTask.run() counted reconnect failures
CUMULATIVELY over the whole process lifetime (retries never reset on a
successful reconnect) and permanently `return`ed after 5, killing the task
with no supervisor. For an intermittently-available server (a Mac that
sleeps under Path A) this meant knowledge_factory died for good after a few
sleep/wake cycles, recoverable only by restarting Hermes.

Required semantics (confirmed with owner):
  - retries counts only CONSECUTIVE failures;
  - a successful connection resets it to 0;
  - after _MAX_RECONNECT_RETRIES the task does NOT end — it keeps retrying
    forever with backoff capped at _MAX_BACKOFF_SECONDS (a warning threshold,
    not a life limit);
  - cancellation and normal shutdown still stop the loop;
  - small jitter so multiple MCP servers don't reconnect in lockstep.
"""
import asyncio
from unittest.mock import patch

import pytest

from tools.mcp_tool import (
    MCPServerTask,
    _MAX_BACKOFF_SECONDS,
    _MAX_RECONNECT_RETRIES,
)


def _drive(coro):
    return asyncio.run(coro)


class _State:
    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.recovered = asyncio.Event()


def _make_run_stdio(state: _State):
    """Return a plain coroutine function (bound as MCPServerTask._run_stdio).

    Scripts behavior per call: 'fail' | 'up' | 'hold'.
      'fail' — raise before connecting (session stays None).
      'up'   — establish (session + _ready) then drop (raise): a healthy
               session that ends; must reset the consecutive counter.
      'hold' — establish and stay up until shutdown; signals recovery.
    """

    async def _run_stdio(task: MCPServerTask, config: dict):
        i = state.calls
        state.calls += 1
        behavior = state.script[i] if i < len(state.script) else "hold"
        if behavior == "fail":
            raise ConnectionError(f"connect failed #{i}")
        task.session = object()
        task._ready.set()
        if behavior == "up":
            raise ConnectionError(f"dropped after connect #{i}")
        state.recovered.set()
        await task._shutdown_event.wait()

    return _run_stdio


async def _no_sleep(_delay):
    return None


def test_success_resets_consecutive_counter(caplog):
    """4 fails, a success, 4 more fails — never 5 CONSECUTIVE, so the
    persistent-failure warning must NOT fire (proves the reset)."""
    state = _State(["up", "fail", "fail", "fail", "fail",
                    "up",
                    "fail", "fail", "fail", "fail",
                    "hold"])
    sleeps = []

    async def rec_sleep(d):
        sleeps.append(d)

    async def scenario():
        server = MCPServerTask("reset-test")
        with patch.object(MCPServerTask, "_run_stdio", _make_run_stdio(state)), \
             patch("tools.mcp_tool.asyncio.sleep", rec_sleep):
            task = asyncio.ensure_future(server.run({"command": "fake"}))
            await asyncio.wait_for(state.recovered.wait(), timeout=5)
            server._shutdown_event.set()
            await asyncio.wait_for(task, timeout=5)

    with caplog.at_level("WARNING"):
        _drive(scenario())

    assert not any("consecutive" in r.message.lower() for r in caplog.records), \
        "persistent-failure warning fired despite a success resetting the counter"


def test_more_than_five_failures_does_not_end_run():
    """The 6th and later consecutive failures must not terminate run()."""
    state = _State(["up"] + ["fail"] * 8 + ["hold"])

    async def scenario():
        server = MCPServerTask("no-giveup")
        with patch.object(MCPServerTask, "_run_stdio", _make_run_stdio(state)), \
             patch("tools.mcp_tool.asyncio.sleep", _no_sleep):
            task = asyncio.ensure_future(server.run({"command": "fake"}))
            await asyncio.wait_for(state.recovered.wait(), timeout=5)
            assert not task.done(), "run() ended instead of continuing to retry"
            server._shutdown_event.set()
            await asyncio.wait_for(task, timeout=5)

    _drive(scenario())


def test_recovers_after_more_than_five_failures():
    """After >5 consecutive failures the server still reconnects."""
    state = _State(["up"] + ["fail"] * 6 + ["hold"])

    async def scenario():
        server = MCPServerTask("late-recovery")
        with patch.object(MCPServerTask, "_run_stdio", _make_run_stdio(state)), \
             patch("tools.mcp_tool.asyncio.sleep", _no_sleep):
            task = asyncio.ensure_future(server.run({"command": "fake"}))
            await asyncio.wait_for(state.recovered.wait(), timeout=5)
            assert server.session is not None, "did not reconnect after >5 failures"
            assert state.calls >= 8  # 1 up + 6 fail + 1 hold
            server._shutdown_event.set()
            await asyncio.wait_for(task, timeout=5)

    _drive(scenario())


def test_backoff_never_exceeds_cap():
    """Every reconnect delay stays within _MAX_BACKOFF_SECONDS (jitter is
    downward, so the cap is never exceeded even after many failures)."""
    state = _State(["up"] + ["fail"] * 12 + ["hold"])
    sleeps = []

    async def rec_sleep(d):
        sleeps.append(d)

    async def scenario():
        server = MCPServerTask("backoff-cap")
        with patch.object(MCPServerTask, "_run_stdio", _make_run_stdio(state)), \
             patch("tools.mcp_tool.asyncio.sleep", rec_sleep):
            task = asyncio.ensure_future(server.run({"command": "fake"}))
            await asyncio.wait_for(state.recovered.wait(), timeout=5)
            server._shutdown_event.set()
            await asyncio.wait_for(task, timeout=5)

    _drive(scenario())

    assert sleeps, "expected reconnect sleeps to be recorded"
    assert max(sleeps) <= _MAX_BACKOFF_SECONDS, \
        f"backoff {max(sleeps)} exceeded cap {_MAX_BACKOFF_SECONDS}"
    assert len(set(sleeps)) > 1, "expected jitter to vary the delays"


def test_cancelled_error_ends_task():
    """CancelledError from the transport must propagate and end run()."""

    async def cancel_run_stdio(self, config):
        raise asyncio.CancelledError()

    async def scenario():
        server = MCPServerTask("cancel-test")
        with patch.object(MCPServerTask, "_run_stdio", cancel_run_stdio), \
             patch("tools.mcp_tool.asyncio.sleep", _no_sleep):
            task = asyncio.ensure_future(server.run({"command": "fake"}))
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

    _drive(scenario())
