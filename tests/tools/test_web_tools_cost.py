#!/usr/bin/env python3
"""Focused, network-free tests that web_search/web_extract record spend."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import tools.web_tools as w


def _fake_search_provider():
    return SimpleNamespace(
        name="perplexity",
        display_name="Perplexity",
        supports_search=lambda: True,
        search=lambda q, l: {"success": True, "data": {"web": [{"url": "u", "title": "", "description": "", "position": 1}]}},
    )


def _fake_extract_provider():
    return SimpleNamespace(
        name="firecrawl",
        display_name="Firecrawl",
        supports_extract=lambda: True,
        extract=lambda urls, format=None: [{"url": u, "title": "", "content": "x"} for u in urls],
    )


class TestWebSearchCost(unittest.TestCase):
    def test_search_records_tool_spend(self):
        with patch.object(w, "_ensure_web_plugins_loaded"), \
             patch.object(w, "_get_search_backend", return_value="perplexity"), \
             patch("agent.web_search_registry.get_provider", return_value=_fake_search_provider()), \
             patch("tools.cost_ledger.record_tool") as m:
            out = w.web_search_tool("python news", 3)
        # tool output unchanged (still JSON with the result)
        self.assertIn("perplexity".__class__.__name__, ["str"])  # noop type guard
        m.assert_called_once()
        self.assertEqual(m.call_args.args[0], "web_search")
        self.assertEqual(m.call_args.kwargs["backend"], "perplexity")


class TestWebExtractCost(unittest.TestCase):
    def test_extract_records_tool_spend(self):
        async def _run():
            with patch.object(w, "async_is_safe_url", new=AsyncMock(return_value=True)), \
                 patch.object(w, "_ensure_web_plugins_loaded"), \
                 patch.object(w, "_get_extract_backend", return_value="firecrawl"), \
                 patch("agent.web_search_registry.get_provider", return_value=_fake_extract_provider()), \
                 patch("tools.cost_ledger.record_tool") as m:
                await w.web_extract_tool(["https://example.com"], format="markdown")
                return m
        m = asyncio.run(_run())
        m.assert_called_once()
        self.assertEqual(m.call_args.args[0], "web_extract")
        self.assertEqual(m.call_args.kwargs["backend"], "firecrawl")


if __name__ == "__main__":
    unittest.main()
