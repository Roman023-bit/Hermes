#!/usr/bin/env python3
"""Tests for the agent-profile resolver (pure, no agent construction)."""

import unittest

from tools.agent_profiles import resolve_profile, list_profiles


def _cfg():
    return {
        "model_aliases": {
            "research": {"model": "google/gemini-3-pro-preview", "provider": "openrouter"},
            "reviewer": {"model": "openai/gpt-5.3-codex", "provider": "openrouter"},
        },
        "agent_profiles": {
            "researcher": {
                "model": "research",
                "toolsets": ["web"],
                "reasoning_effort": "medium",
                "prompt": "You are a rigorous researcher.",
            },
            "coder": {
                "model": "anthropic/claude-sonnet-4.6",  # literal provider/model
            },
            "forced": {
                "model": "research",
                "provider": "nous",  # explicit provider overrides alias provider
            },
        },
    }


class TestResolveProfile(unittest.TestCase):
    def test_known_profile_full_fields(self):
        p = resolve_profile("researcher", _cfg())
        self.assertEqual(p["model"], "google/gemini-3-pro-preview")
        self.assertEqual(p["provider"], "openrouter")
        self.assertEqual(p["toolsets"], ["web"])
        self.assertEqual(p["reasoning_effort"], "medium")
        self.assertEqual(p["prompt"], "You are a rigorous researcher.")

    def test_model_is_alias_resolved(self):
        p = resolve_profile("researcher", _cfg())
        self.assertEqual(p["model"], "google/gemini-3-pro-preview")

    def test_model_is_literal_when_not_an_alias(self):
        p = resolve_profile("coder", _cfg())
        self.assertEqual(p["model"], "anthropic/claude-sonnet-4.6")
        self.assertIsNone(p["provider"])

    def test_explicit_provider_overrides_alias_provider(self):
        p = resolve_profile("forced", _cfg())
        self.assertEqual(p["model"], "google/gemini-3-pro-preview")
        self.assertEqual(p["provider"], "nous")

    def test_unknown_profile_returns_none(self):
        self.assertIsNone(resolve_profile("nope", _cfg()))

    def test_missing_block_returns_none(self):
        self.assertIsNone(resolve_profile("researcher", {}))

    def test_none_name_returns_none(self):
        self.assertIsNone(resolve_profile(None, _cfg()))


class TestListProfiles(unittest.TestCase):
    def test_lists_sorted_names(self):
        self.assertEqual(list_profiles(_cfg()), ["coder", "forced", "researcher"])

    def test_empty_when_missing(self):
        self.assertEqual(list_profiles({}), [])


if __name__ == "__main__":
    unittest.main()
