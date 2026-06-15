#!/usr/bin/env python3
"""Tests for the cost ledger and tool pricing modules (unittest).

Converted from pytest style to stdlib unittest so it runs under
`python -m unittest tests.tools.test_cost_ledger` in environments without
pytest. Coverage is unchanged: the pytest `isolated_store` autouse fixture
is reproduced via setUp/tearDown (temp store + reset + force-enable), and
`pytest.approx` is replaced with `assertAlmostEqual`.
"""

import json
import tempfile
import unittest
from pathlib import Path

import tools.cost_ledger as L
import tools.tool_pricing as P


class _LedgerTestBase(unittest.TestCase):
    """Point the lifetime store at a temp file and reset in-process state.

    Mirrors the original `isolated_store` autouse fixture.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = Path(self._tmpdir.name) / "spend.json"
        self._orig_store_path = L._store_path
        L._store_path = lambda: self.store
        L._reset_for_tests()
        L._enabled_cache = True  # force-enable regardless of config.yaml

    def tearDown(self):
        L._reset_for_tests()
        L._store_path = self._orig_store_path
        self._tmpdir.cleanup()


class TestLedgerAggregation(_LedgerTestBase):
    def test_turn_aggregates_tools_models_and_total(self):
        L.begin_turn()
        L.record_tool("web_search", backend="perplexity", amount_usd=0.006, units="1 search")
        L.record_tool("web_extract", backend="firecrawl", models=["gemini-3-flash"],
                      amount_usd=0.002, units="2 urls")
        L.record_llm("deepseek-v4-pro", amount_usd=0.004, role="main")
        summary = L.end_turn()

        self.assertAlmostEqual(summary.turn_total_usd, 0.012)
        self.assertAlmostEqual(summary.session_total_usd, 0.012)
        self.assertEqual(summary.tools, [("web_search", "perplexity"), ("web_extract", "firecrawl")])
        self.assertIn("deepseek-v4-pro", summary.models)
        self.assertIn("gemini-3-flash", summary.models)

    def test_session_accumulates_across_turns(self):
        L.begin_turn()
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        L.end_turn()
        L.begin_turn()
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        s2 = L.end_turn()
        self.assertAlmostEqual(s2.session_total_usd, 0.010)

    def test_begin_turn_clears_previous_entries(self):
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        L.begin_turn()  # discards the stray entry above
        summary = L.end_turn()
        self.assertTrue(summary.is_empty)
        self.assertEqual(summary.turn_total_usd, 0.0)

    def test_unknown_status_flagged(self):
        L.begin_turn()
        L.record_tool("mystery", amount_usd=None, status=L.STATUS_UNKNOWN)
        summary = L.end_turn()
        self.assertIs(summary.has_unknown, True)


class TestLifetimePersistence(_LedgerTestBase):
    def test_lifetime_persists_to_store(self):
        L.begin_turn()
        L.record_tool("web_search", backend="perplexity", amount_usd=0.006)
        summary = L.end_turn()
        self.assertAlmostEqual(summary.lifetime_total_usd, 0.006)

        with open(self.store, encoding="utf-8") as f:
            data = json.load(f)
        self.assertAlmostEqual(data["lifetime_usd"], 0.006)
        self.assertAlmostEqual(data["by_source"]["perplexity"], 0.006)
        self.assertAlmostEqual(sum(data["by_day"].values()), 0.006)

    def test_lifetime_carries_into_new_process_state(self):
        L.begin_turn()
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        L.end_turn()
        # Simulate a fresh process: clear in-memory session, keep the file.
        L._reset_for_tests()
        L._enabled_cache = True
        self.assertAlmostEqual(L.lifetime_total_usd(), 0.005)
        top = L.top_sources()[0]
        self.assertEqual(top[0], "exa")
        self.assertAlmostEqual(top[1], 0.005)

    def test_llm_grouped_by_model_in_breakdown(self):
        L.begin_turn()
        L.record_llm("deepseek-v4-pro", amount_usd=0.02, role="main")
        L.end_turn()
        with open(self.store, encoding="utf-8") as f:
            data = json.load(f)
        self.assertAlmostEqual(data["by_source"]["deepseek-v4-pro"], 0.02)


class TestDisabled(_LedgerTestBase):
    def test_disabled_records_nothing(self):
        L._enabled_cache = False
        L.begin_turn()
        L.record_tool("web_search", backend="perplexity", amount_usd=0.006)
        summary = L.end_turn()
        self.assertTrue(summary.is_empty)


class TestFooter(_LedgerTestBase):
    def test_footer_contains_tools_models_and_costs(self):
        L.begin_turn()
        L.record_tool("web_search", backend="perplexity", amount_usd=0.006)
        L.record_llm("deepseek-v4-pro", amount_usd=0.004, role="main")
        summary = L.end_turn()
        footer = L.render_footer(summary)
        self.assertIn("web_search(perplexity)", footer)
        self.assertIn("deepseek-v4-pro", footer)
        self.assertIn("ход:", footer)
        self.assertIn("сессия:", footer)
        self.assertIn("всего:", footer)

    def test_empty_turn_renders_blank(self):
        L.begin_turn()
        summary = L.end_turn()
        self.assertEqual(L.render_footer(summary), "")


class TestSpendReport(_LedgerTestBase):
    def test_report_empty_when_no_spend(self):
        self.assertIn("нет учтённых расходов", L.render_spend_report())

    def test_report_lists_lifetime_and_top_sources(self):
        L.begin_turn()
        L.record_llm("deepseek-v4-pro", amount_usd=28.10, role="main")
        L.record_tool("web_search", backend="perplexity", amount_usd=7.40)
        L.end_turn()
        report = L.render_spend_report()
        self.assertIn("$35.50", report)  # 28.10 + 7.40
        self.assertIn("deepseek-v4-pro", report)
        self.assertIn("perplexity", report)

    def test_markdown_variant_uses_bullets(self):
        L.begin_turn()
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        L.end_turn()
        report = L.render_spend_report(markdown=True)
        self.assertTrue(report.startswith("## "))
        self.assertIn("- ", report)


class TestPricing(_LedgerTestBase):
    def test_search_prices(self):
        self.assertAlmostEqual(P.search_cost("exa")[0], 0.005)
        self.assertAlmostEqual(P.search_cost("perplexity")[0], 0.006)
        amt, status, _ = P.search_cost("unknown-backend")
        self.assertIsNone(amt)
        self.assertEqual(status, L.STATUS_ESTIMATED)

    def test_perplexity_token_costing_when_usage_present(self):
        amt, _, units = P.search_cost("perplexity", usage={"prompt_tokens": 1000, "completion_tokens": 500})
        self.assertAlmostEqual(amt, 0.0015)
        self.assertIn("tokens", units)

    def test_extract_scales_with_urls(self):
        self.assertAlmostEqual(P.extract_cost("firecrawl", 3)[0], 0.003)
        self.assertEqual(P.extract_cost("firecrawl", 0)[0], 0.0)

    def test_image_scales_with_count(self):
        self.assertAlmostEqual(P.image_cost("fal", 3)[0], 0.12)

    def test_free_tts_is_included(self):
        amt, status, _ = P.tts_cost("edge", 1000)
        self.assertEqual(amt, 0.0)
        self.assertEqual(status, L.STATUS_INCLUDED)

    def test_paid_tts_scales_with_chars(self):
        amt, status, _ = P.tts_cost("elevenlabs", 1000)
        self.assertAlmostEqual(amt, 0.03)
        self.assertEqual(status, L.STATUS_ESTIMATED)


if __name__ == "__main__":
    unittest.main()
