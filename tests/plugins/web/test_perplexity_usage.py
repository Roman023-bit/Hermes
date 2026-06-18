#!/usr/bin/env python3
"""Network-free tests for Perplexity usage propagation + token pricing.

Covers the #3 fix: the Sonar /chat/completions fallback spends tokens, so its
``usage`` must reach ``tool_pricing`` to be priced by tokens instead of the
flat per-request fallback. The dedicated /search endpoint reports no usage and
must keep its previous (flat-price) behaviour.
"""

import unittest
from unittest.mock import patch

import plugins.web.perplexity.provider as ppx
from tools import tool_pricing


class _FakeResp:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class TestPerplexityUsagePropagation(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict("os.environ", {"PERPLEXITY_API_KEY": "test-key"})
        self._env.start()
        self.provider = ppx.PerplexityWebSearchProvider()

    def tearDown(self):
        self._env.stop()

    def test_search_endpoint_without_usage_unchanged(self):
        """/search returns ranked links and no usage → result has no 'usage'."""
        search_payload = {
            "results": [
                {"title": "T", "url": "https://a", "snippet": "s"},
            ]
        }
        with patch.object(ppx.httpx, "post", return_value=_FakeResp(200, search_payload)) as m:
            result = self.provider.search("query", limit=3)

        m.assert_called_once()  # only /search, no Sonar fallback
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["web"][0]["url"], "https://a")
        self.assertNotIn("usage", result)

    def test_sonar_fallback_propagates_usage(self):
        """/search 403 → Sonar fallback → usage carried into the result."""
        sonar_payload = {
            "search_results": [{"title": "T", "url": "https://a"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200},
        }
        responses = [
            _FakeResp(403, {}),            # /search: no access → fall through
            _FakeResp(200, sonar_payload),  # /chat/completions
        ]
        with patch.object(ppx.httpx, "post", side_effect=responses):
            result = self.provider.search("query", limit=3)

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["web"][0]["url"], "https://a")
        self.assertIn("usage", result)
        self.assertEqual(result["usage"]["prompt_tokens"], 100)
        self.assertEqual(result["usage"]["completion_tokens"], 200)


class TestPerplexityTokenPricing(unittest.TestCase):
    def test_token_cost_used_when_usage_present(self):
        """With usage, search_cost prices by tokens (not the flat fallback)."""
        amount, status, units = tool_pricing.search_cost(
            "perplexity",
            usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
        # 1M in + 1M out at $1/M each = $2.00, far from the $0.006 flat price.
        self.assertAlmostEqual(amount, 2.0)
        self.assertEqual(status, tool_pricing.STATUS_ESTIMATED)
        self.assertIn("tokens", units)

    def test_flat_price_without_usage(self):
        """Without usage, perplexity keeps the flat per-request estimate."""
        amount, _status, units = tool_pricing.search_cost("perplexity")
        self.assertAlmostEqual(amount, 0.006)
        self.assertNotIn("tokens", units)

    def test_empty_usage_falls_back_to_flat(self):
        """Zero-token usage → no token cost → flat per-request price."""
        amount, _status, _units = tool_pricing.search_cost(
            "perplexity",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )
        self.assertAlmostEqual(amount, 0.006)


if __name__ == "__main__":
    unittest.main()
