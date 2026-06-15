#!/usr/bin/env python3
"""Focused test: the gateway /spend handler returns the cost-ledger report.

No Telegram/API — the handler ignores self/event, so it is called directly
with throwaway stubs and the cost ledger is mocked.
"""

import asyncio
import unittest
from unittest.mock import patch

from gateway.slash_commands import GatewaySlashCommandsMixin


class TestGatewaySpendCommand(unittest.TestCase):
    def test_returns_spend_report_markdown(self):
        with patch("tools.cost_ledger.render_spend_report", return_value="REPORT-OK") as m:
            out = asyncio.run(
                GatewaySlashCommandsMixin._handle_spend_command(object(), object())
            )
        m.assert_called_once()
        self.assertIs(m.call_args.kwargs.get("markdown"), True)
        self.assertEqual(out, "REPORT-OK")

    def test_best_effort_on_error_returns_message_not_raise(self):
        with patch("tools.cost_ledger.render_spend_report", side_effect=RuntimeError("boom")):
            out = asyncio.run(
                GatewaySlashCommandsMixin._handle_spend_command(object(), object())
            )
        self.assertIsInstance(out, str)
        self.assertTrue(out.strip())  # friendly fallback, not an exception


if __name__ == "__main__":
    unittest.main()
