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


class TestModelBasedImagePricing(unittest.TestCase):
    """Per-model image pricing: premium models cost more than the backend flat rate."""

    def test_gpt_image_2_priced_by_model(self):
        from tools import tool_pricing
        with patch.object(tool_pricing, "_config_overrides", return_value={}):
            amount, status, units = tool_pricing.image_cost(
                "replicate", 1, model="openai/gpt-image-2"
            )
        self.assertAlmostEqual(amount, 0.21)
        self.assertEqual(units, "1 image")

    def test_unknown_model_falls_back_to_backend_price(self):
        from tools import tool_pricing
        with patch.object(tool_pricing, "_config_overrides", return_value={}):
            amount, _, _ = tool_pricing.image_cost(
                "replicate", 1, model="black-forest-labs/flux-1.1-pro"
            )
        self.assertAlmostEqual(amount, 0.04)

    def test_no_model_stays_backward_compatible(self):
        from tools import tool_pricing
        with patch.object(tool_pricing, "_config_overrides", return_value={}):
            amount, _, units = tool_pricing.image_cost("fal", 2)
        self.assertAlmostEqual(amount, 0.08)
        self.assertEqual(units, "2 images")

    def test_config_override_by_model_slug_wins(self):
        from tools import tool_pricing
        overrides = {"image": {"openai/gpt-image-2": 0.5}}
        with patch.object(tool_pricing, "_config_overrides", return_value=overrides):
            amount, _, _ = tool_pricing.image_cost(
                "replicate", 1, model="openai/gpt-image-2"
            )
        self.assertAlmostEqual(amount, 0.5)

    def test_record_helper_passes_model_from_payload(self):
        with patch("tools.tool_pricing.image_cost",
                   return_value=(0.21, "estimated", "1 image")) as m, \
             patch("tools.cost_ledger.record_tool"):
            ig._record_image_cost(
                '{"success": true, "image": "/x.png", "model": "openai/gpt-image-2"}',
                "replicate", {"num_images": 1},
            )
        self.assertEqual(m.call_args.kwargs.get("model"), "openai/gpt-image-2")


if __name__ == "__main__":
    unittest.main()
