#!/usr/bin/env python3
"""Focused, network-free test that TTS records spend after a save."""

import unittest
from unittest.mock import patch

import tools.tts_tool as t


class TestTtsCost(unittest.TestCase):
    def test_records_tts_spend(self):
        with patch("tools.cost_ledger.record_tool") as m:
            t._record_tts_cost("openai", "hello world")
        m.assert_called_once()
        self.assertEqual(m.call_args.args[0], "text_to_speech")
        self.assertEqual(m.call_args.kwargs["backend"], "openai")
        # units/amount come from real tool_pricing.tts_cost (config lookup, no network)
        self.assertIn("units", m.call_args.kwargs)

    def test_never_raises(self):
        # record_tool blows up -> helper must swallow it
        with patch("tools.cost_ledger.record_tool", side_effect=RuntimeError("boom")):
            t._record_tts_cost("openai", "hi")  # must not raise


if __name__ == "__main__":
    unittest.main()
