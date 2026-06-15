#!/usr/bin/env python3
"""Focused test: the Codex app-server usage path records LLM spend."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import agent.codex_runtime as cr


def _fake_agent():
    return SimpleNamespace(
        model="gpt-5.3-codex", provider="openrouter", base_url="", api_key="",
        session_api_calls=0,
        session_prompt_tokens=0, session_completion_tokens=0, session_total_tokens=0,
        session_input_tokens=0, session_output_tokens=0,
        session_cache_read_tokens=0, session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="unknown", session_cost_source="none",
        context_compressor=None, _session_db=None, session_id=None,
        _session_db_created=False,
    )


class TestCodexLlmCostHook(unittest.TestCase):
    def test_records_llm_for_turn(self):
        ag = _fake_agent()
        turn = SimpleNamespace(
            token_usage_last={
                "inputTokens": 10, "outputTokens": 5, "totalTokens": 15,
                "cachedInputTokens": 0, "reasoningOutputTokens": 0,
            },
            model_context_window=None,
        )
        fake_cost = SimpleNamespace(amount_usd=0.002, status="estimated", source="openrouter")
        with patch("agent.usage_pricing.estimate_usage_cost", return_value=fake_cost), \
             patch("tools.cost_ledger.record_llm_for_turn") as m:
            cr._record_codex_app_server_usage(ag, turn)
        m.assert_called_once_with(ag.model, ag.provider, fake_cost, role="main")


if __name__ == "__main__":
    unittest.main()
