#!/usr/bin/env python3
"""Focused tests for agent-profile support in delegate_tool.

No live agents / API: `_build_child_agent` is patched to capture kwargs and
short-circuit, and credential resolution is stubbed. Covers:
  - _merge_profile_into_cfg overlay semantics
  - schema exposes `profile` (static + dynamic rebuild)
  - delegate_task WITHOUT profile behaves as before (default delegation model)
  - delegate_task WITH profile applies model/toolset/prompt
"""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tools.delegate_tool as dt
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, _merge_profile_into_cfg


def _make_mock_parent(depth=0):
    return SimpleNamespace(
        base_url="https://openrouter.ai/api/v1", api_key="***", provider="openrouter",
        api_mode="chat_completions", model="anthropic/claude-sonnet-4", platform="cli",
        _session_db=None, _delegate_depth=depth, _active_children=[],
        _active_children_lock=threading.Lock(), _print_fn=None,
        tool_progress_callback=None, thinking_callback=None,
        enabled_toolsets=["terminal", "file", "web"],
    )


class TestMergeProfileIntoCfg(unittest.TestCase):
    def test_profile_overrides_model_and_provider(self):
        base = {"model": "openai/gpt-5.3-codex", "provider": "openrouter", "max_iterations": 50}
        merged = _merge_profile_into_cfg(base, {"model": "google/gemini-3-pro-preview", "provider": "nous"})
        self.assertEqual(merged["model"], "google/gemini-3-pro-preview")
        self.assertEqual(merged["provider"], "nous")
        self.assertEqual(merged["max_iterations"], 50)
        self.assertEqual(base["model"], "openai/gpt-5.3-codex")  # base not mutated

    def test_none_profile_returns_copy(self):
        base = {"model": "x", "provider": "y"}
        merged = _merge_profile_into_cfg(base, None)
        self.assertEqual(merged, base)
        self.assertIsNot(merged, base)

    def test_profile_without_provider_keeps_base(self):
        merged = _merge_profile_into_cfg({"model": "x", "provider": "openrouter"}, {"model": "z", "provider": None})
        self.assertEqual(merged["model"], "z")
        self.assertEqual(merged["provider"], "openrouter")


class TestSchemaHasProfile(unittest.TestCase):
    def test_static_schema(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("profile", props)
        self.assertEqual(props["profile"]["type"], "string")
        self.assertIn("profile", props["tasks"]["items"]["properties"])
        # role axis untouched
        self.assertEqual(props["role"]["enum"], ["leaf", "orchestrator"])

    def test_dynamic_rebuild_keeps_profile(self):
        ov_fn = getattr(dt, "_build_dynamic_schema_overrides", None)
        if ov_fn is None:
            self.skipTest("no dynamic schema rebuild in this version")
        ov = ov_fn()
        self.assertIn("profile", ov["parameters"]["properties"])


class TestDelegateProfileWiring(unittest.TestCase):
    """delegate_task resolves per-task profile -> model/toolset/prompt before build."""

    class _StopBuild(Exception):
        pass

    def _run(self, tasks=None, goal=None, profile=None):
        captured = []

        def _fake_build(**kw):
            captured.append(kw)
            raise self._StopBuild()

        full_cfg = {
            "delegation": {"model": "openai/gpt-5.3-codex", "provider": "openrouter", "max_iterations": 50},
            "model_aliases": {"research": {"model": "google/gemini-3-pro-preview", "provider": "openrouter"}},
            "agent_profiles": {
                "researcher": {"model": "research", "toolsets": ["web"], "prompt": "You are a researcher."},
            },
        }
        parent = _make_mock_parent()
        with patch.object(dt, "_load_config", return_value=full_cfg["delegation"]), \
             patch("tools.agent_profiles.load_full_config", return_value=full_cfg), \
             patch.object(dt, "_resolve_delegation_credentials",
                          side_effect=lambda cfg, pa: {
                              "model": cfg.get("model"), "provider": cfg.get("provider"),
                              "base_url": None, "api_key": None, "api_mode": None,
                          }), \
             patch.object(dt, "_build_child_agent", side_effect=_fake_build):
            try:
                dt.delegate_task(tasks=tasks, goal=goal, profile=profile, parent_agent=parent)
            except self._StopBuild:
                pass
        return captured

    def test_no_profile_uses_delegation_default(self):
        cap = self._run(goal="plain task")
        self.assertEqual(cap[0]["model"], "openai/gpt-5.3-codex")
        self.assertIsNone(cap[0].get("profile_prompt"))

    def test_profile_applies_model_toolset_prompt(self):
        cap = self._run(goal="research X", profile="researcher")
        self.assertEqual(cap[0]["model"], "google/gemini-3-pro-preview")
        self.assertEqual(cap[0]["toolsets"], ["web"])
        self.assertEqual(cap[0]["profile_prompt"], "You are a researcher.")

    def test_per_task_profile_overrides_top(self):
        cap = self._run(tasks=[{"goal": "a", "profile": "researcher"}])
        self.assertEqual(cap[0]["model"], "google/gemini-3-pro-preview")


if __name__ == "__main__":
    unittest.main()
