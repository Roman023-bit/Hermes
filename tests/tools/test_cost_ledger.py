"""Tests for the cost ledger and tool pricing modules."""

import json

import pytest

import tools.cost_ledger as L
import tools.tool_pricing as P


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the lifetime store at a temp file and reset in-process state."""
    store = tmp_path / "spend.json"
    monkeypatch.setattr(L, "_store_path", lambda: store)
    L._reset_for_tests()
    L._enabled_cache = True  # force-enable regardless of config.yaml
    yield store
    L._reset_for_tests()


class TestLedgerAggregation:
    def test_turn_aggregates_tools_models_and_total(self):
        L.begin_turn()
        L.record_tool("web_search", backend="perplexity", amount_usd=0.006, units="1 search")
        L.record_tool("web_extract", backend="firecrawl", models=["gemini-3-flash"],
                      amount_usd=0.002, units="2 urls")
        L.record_llm("deepseek-v4-pro", amount_usd=0.004, role="main")
        summary = L.end_turn()

        assert summary.turn_total_usd == pytest.approx(0.012)
        assert summary.session_total_usd == pytest.approx(0.012)
        assert summary.tools == [("web_search", "perplexity"), ("web_extract", "firecrawl")]
        assert "deepseek-v4-pro" in summary.models
        assert "gemini-3-flash" in summary.models

    def test_session_accumulates_across_turns(self):
        L.begin_turn()
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        L.end_turn()
        L.begin_turn()
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        s2 = L.end_turn()
        assert s2.session_total_usd == pytest.approx(0.010)

    def test_begin_turn_clears_previous_entries(self):
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        L.begin_turn()  # discards the stray entry above
        summary = L.end_turn()
        assert summary.is_empty
        assert summary.turn_total_usd == 0.0

    def test_unknown_status_flagged(self):
        L.begin_turn()
        L.record_tool("mystery", amount_usd=None, status=L.STATUS_UNKNOWN)
        summary = L.end_turn()
        assert summary.has_unknown is True


class TestLifetimePersistence:
    def test_lifetime_persists_to_store(self, isolated_store):
        L.begin_turn()
        L.record_tool("web_search", backend="perplexity", amount_usd=0.006)
        summary = L.end_turn()
        assert summary.lifetime_total_usd == pytest.approx(0.006)

        data = json.loads(isolated_store.read_text())
        assert data["lifetime_usd"] == pytest.approx(0.006)
        assert data["by_source"]["perplexity"] == pytest.approx(0.006)
        assert sum(data["by_day"].values()) == pytest.approx(0.006)

    def test_lifetime_carries_into_new_process_state(self, isolated_store):
        L.begin_turn()
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        L.end_turn()
        # Simulate a fresh process: clear in-memory session, keep the file.
        L._reset_for_tests()
        L._enabled_cache = True
        assert L.lifetime_total_usd() == pytest.approx(0.005)
        assert L.top_sources()[0] == ("exa", pytest.approx(0.005))

    def test_llm_grouped_by_model_in_breakdown(self, isolated_store):
        L.begin_turn()
        L.record_llm("deepseek-v4-pro", amount_usd=0.02, role="main")
        L.end_turn()
        data = json.loads(isolated_store.read_text())
        assert data["by_source"]["deepseek-v4-pro"] == pytest.approx(0.02)


class TestDisabled:
    def test_disabled_records_nothing(self):
        L._enabled_cache = False
        L.begin_turn()
        L.record_tool("web_search", backend="perplexity", amount_usd=0.006)
        summary = L.end_turn()
        assert summary.is_empty


class TestFooter:
    def test_footer_contains_tools_models_and_costs(self):
        L.begin_turn()
        L.record_tool("web_search", backend="perplexity", amount_usd=0.006)
        L.record_llm("deepseek-v4-pro", amount_usd=0.004, role="main")
        summary = L.end_turn()
        footer = L.render_footer(summary)
        assert "web_search(perplexity)" in footer
        assert "deepseek-v4-pro" in footer
        assert "ход:" in footer and "сессия:" in footer and "всего:" in footer

    def test_empty_turn_renders_blank(self):
        L.begin_turn()
        summary = L.end_turn()
        assert L.render_footer(summary) == ""


class TestSpendReport:
    def test_report_empty_when_no_spend(self):
        assert "нет учтённых расходов" in L.render_spend_report()

    def test_report_lists_lifetime_and_top_sources(self):
        L.begin_turn()
        L.record_llm("deepseek-v4-pro", amount_usd=28.10, role="main")
        L.record_tool("web_search", backend="perplexity", amount_usd=7.40)
        L.end_turn()
        report = L.render_spend_report()
        assert "$35.50" in report  # 28.10 + 7.40
        assert "deepseek-v4-pro" in report
        assert "perplexity" in report

    def test_markdown_variant_uses_bullets(self):
        L.begin_turn()
        L.record_tool("web_search", backend="exa", amount_usd=0.005)
        L.end_turn()
        report = L.render_spend_report(markdown=True)
        assert report.startswith("## ")
        assert "- " in report


class TestPricing:
    def test_search_prices(self):
        assert P.search_cost("exa")[0] == pytest.approx(0.005)
        assert P.search_cost("perplexity")[0] == pytest.approx(0.006)
        amt, status, _ = P.search_cost("unknown-backend")
        assert amt is None and status == L.STATUS_ESTIMATED

    def test_perplexity_token_costing_when_usage_present(self):
        amt, _, units = P.search_cost("perplexity", usage={"prompt_tokens": 1000, "completion_tokens": 500})
        assert amt == pytest.approx(0.0015)
        assert "tokens" in units

    def test_extract_scales_with_urls(self):
        assert P.extract_cost("firecrawl", 3)[0] == pytest.approx(0.003)
        assert P.extract_cost("firecrawl", 0)[0] == 0.0

    def test_image_scales_with_count(self):
        assert P.image_cost("fal", 3)[0] == pytest.approx(0.12)

    def test_free_tts_is_included(self):
        amt, status, _ = P.tts_cost("edge", 1000)
        assert amt == 0.0 and status == L.STATUS_INCLUDED

    def test_paid_tts_scales_with_chars(self):
        amt, status, _ = P.tts_cost("elevenlabs", 1000)
        assert amt == pytest.approx(0.03) and status == L.STATUS_ESTIMATED
