#!/usr/bin/env python3
"""Network-free tests for Perplexity usage propagation.

Covers the Sonar fallback contract: chat completions spend tokens, so their
``usage`` metadata must survive provider normalization. The dedicated search
endpoint reports no usage and must keep its previous behaviour.
"""

import unittest
from unittest.mock import patch

import plugins.web.perplexity.provider as ppx


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

if __name__ == "__main__":
    unittest.main()
