#!/usr/bin/env python3
"""Unit tests for cost_ledger.record_llm_for_turn (glue, no live turn)."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools import cost_ledger


class TestRecordLlmForTurn(unittest.TestCase):
    def test_records_with_float_amount(self):
        cr = SimpleNamespace(amount_usd=0.0123, status="estimated")
        with patch.object(cost_ledger, "record_llm") as m:
            cost_ledger.record_llm_for_turn("gpt-x", "openrouter", cr, role="main")
        m.assert_called_once()
        self.assertEqual(m.call_args.args[0], "gpt-x")
        kw = m.call_args.kwargs
        self.assertAlmostEqual(kw["amount_usd"], 0.0123)
        self.assertEqual(kw["status"], "estimated")
        self.assertEqual(kw["role"], "main")
        self.assertEqual(kw["provider"], "openrouter")

    def test_none_amount_passes_through(self):
        cr = SimpleNamespace(amount_usd=None, status="unknown")
        with patch.object(cost_ledger, "record_llm") as m:
            cost_ledger.record_llm_for_turn("m", "p", cr)
        self.assertIsNone(m.call_args.kwargs["amount_usd"])
        self.assertEqual(m.call_args.kwargs["role"], "main")  # default

    def test_never_raises_when_record_fails(self):
        with patch.object(cost_ledger, "record_llm", side_effect=RuntimeError("boom")):
            # must not propagate
            cost_ledger.record_llm_for_turn("m", "p", SimpleNamespace(amount_usd=1.0, status="estimated"))


if __name__ == "__main__":
    unittest.main()
