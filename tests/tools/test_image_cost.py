#!/usr/bin/env python3
"""Focused, no-API tests: image generation records spend on BOTH return paths."""

import unittest
from unittest.mock import patch

import tools.image_generation_tool as ig


class TestRecordImageCostHelper(unittest.TestCase):
    def test_success_records(self):
        with patch("tools.cost_ledger.record_tool") as m:
            ig._record_image_cost('{"success": true, "image": "/x.png"}', "openai", {"num_images": 2})
        m.assert_called_once()
        self.assertEqual(m.call_args.args[0], "image_generate")
        self.assertEqual(m.call_args.kwargs["backend"], "openai")

    def test_failure_does_not_record(self):
        with patch("tools.cost_ledger.record_tool") as m:
            ig._record_image_cost('{"success": false, "error": "x"}', "fal", {})
        m.assert_not_called()

    def test_never_raises(self):
        with patch("tools.cost_ledger.record_tool", side_effect=RuntimeError("boom")):
            ig._record_image_cost('{"success": true}', "fal", {"num_images": 1})


class TestHandleImageGenerateBothPaths(unittest.TestCase):
    def test_default_fal_path_records_and_payload_unchanged(self):
        ok = '{"success": true, "image": "/a.png"}'
        with patch.object(ig, "_dispatch_to_plugin_provider", return_value=None), \
             patch.object(ig, "image_generate_tool", return_value=ok), \
             patch.object(ig, "_postprocess_image_generate_result", side_effect=lambda r, task_id=None: r), \
             patch("tools.cost_ledger.record_tool") as m:
            out = ig._handle_image_generate({"prompt": "cat", "num_images": 1})
        self.assertEqual(out, ok)  # payload UNCHANGED
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs["backend"], "fal")

    def test_plugin_dispatch_path_records_and_payload_unchanged(self):
        ok = '{"success": true, "image": "/b.png"}'
        with patch.object(ig, "_dispatch_to_plugin_provider", return_value=ok), \
             patch.object(ig, "_read_configured_image_provider", return_value="openai"), \
             patch.object(ig, "_postprocess_image_generate_result", side_effect=lambda r, task_id=None: r), \
             patch("tools.cost_ledger.record_tool") as m:
            out = ig._handle_image_generate({"prompt": "dog"})
        self.assertEqual(out, ok)  # payload UNCHANGED
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs["backend"], "openai")


if __name__ == "__main__":
    unittest.main()
