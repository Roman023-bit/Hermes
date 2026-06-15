#!/usr/bin/env python3
"""Focused test of the spend-footer gate (no full-CLI boot)."""

import unittest

import cli


class TestSpendFooterGate(unittest.TestCase):
    def test_default_on_when_none(self):
        self.assertTrue(cli._spend_footer_enabled(None))

    def test_default_on_when_empty(self):
        self.assertTrue(cli._spend_footer_enabled({}))

    def test_off_when_false(self):
        self.assertFalse(cli._spend_footer_enabled({"show_footer": False}))

    def test_on_when_true(self):
        self.assertTrue(cli._spend_footer_enabled({"show_footer": True}))

    def test_on_when_malformed(self):
        self.assertTrue(cli._spend_footer_enabled("not-a-dict"))


if __name__ == "__main__":
    unittest.main()
