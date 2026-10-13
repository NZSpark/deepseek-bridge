"""Token-speed estimates: numerical behavior and privacy-safe serialization."""

import json
import math
import unittest

from deepseek_web.token_speed import SpeedTiming, compute_speed


class ComputeSpeedTests(unittest.TestCase):
    def test_calculates_rates_with_polling_and_settle_adjustments(self):
        speed = compute_speed(
            prompt="abcd",
            reply="abcdefgh",
            timing=SpeedTiming(
                submitted_at=10.0,
                first_text_at=12.0,
                finished_at=16.0,
                poll_interval_s=1.0,
                settle_credit_s=1.5,
                saw_generating=True,
                streamed=True,
            ),
        )

        self.assertEqual(speed.prompt_chars, 4)
        self.assertEqual(speed.reply_chars, 8)
        self.assertEqual(speed.prompt_tokens, 1)
        self.assertEqual(speed.reply_tokens, 2)
        self.assertAlmostEqual(speed.ttft_s, 2.0)
        self.assertAlmostEqual(speed.decode_s, 4.0)
        self.assertAlmostEqual(speed.total_s, 6.0)
        self.assertAlmostEqual(speed.poll_credit_s, 0.5)
        self.assertAlmostEqual(speed.settle_credit_s, 1.5)
        self.assertAlmostEqual(speed.ttft_adj_s, 1.5)
        self.assertAlmostEqual(speed.decode_adj_s, 2.5)
        self.assertAlmostEqual(speed.prefill_tps, 1 / 1.5)
        self.assertAlmostEqual(speed.decode_tps, 2 / 2.5)
        self.assertAlmostEqual(speed.bridge_overhead_ratio, 1 / 3)
        self.assertTrue(speed.saw_generating)
        self.assertTrue(speed.streamed)

    def test_missing_first_text_has_zero_decode_rate(self):
        speed = compute_speed(
            prompt="abcd",
            reply="",
            timing=SpeedTiming(
                submitted_at=5.0,
                first_text_at=None,
                finished_at=8.0,
                poll_interval_s=1.0,
            ),
        )

        self.assertAlmostEqual(speed.ttft_s, 3.0)
        self.assertEqual(speed.decode_s, 0.0)
        self.assertEqual(speed.reply_tokens, 0)
        self.assertEqual(speed.decode_tps, 0.0)
        self.assertAlmostEqual(speed.poll_credit_s, 0.5)
        self.assertAlmostEqual(speed.ttft_adj_s, 2.5)

    def test_adjustments_are_clamped_to_observed_intervals(self):
        speed = compute_speed(
            prompt="",
            reply="abcdefgh",
            timing=SpeedTiming(
                submitted_at=1.0,
                first_text_at=2.0,
                finished_at=3.0,
                poll_interval_s=100.0,
                settle_credit_s=100.0,
            ),
        )

        self.assertAlmostEqual(speed.poll_credit_s, 1.0)
        self.assertAlmostEqual(speed.settle_credit_s, 1.0)
        self.assertGreater(speed.ttft_adj_s, 0.0)
        self.assertGreater(speed.decode_adj_s, 0.0)
        self.assertTrue(math.isfinite(speed.prefill_tps))
        self.assertTrue(math.isfinite(speed.decode_tps))
        self.assertGreaterEqual(speed.bridge_overhead_ratio, 0.0)
        self.assertLessEqual(speed.bridge_overhead_ratio, 1.0)

    def test_negative_intervals_and_credits_are_safe(self):
        speed = compute_speed(
            prompt="",
            reply="aaaa",
            timing=SpeedTiming(
                submitted_at=2.0,
                first_text_at=1.0,
                finished_at=0.0,
                poll_interval_s=-1.0,
                settle_credit_s=-5.0,
            ),
        )

        self.assertEqual(speed.total_s, 0.0)
        self.assertEqual(speed.ttft_s, 0.0)
        self.assertEqual(speed.decode_s, 0.0)
        self.assertEqual(speed.poll_credit_s, 0.0)
        self.assertEqual(speed.settle_credit_s, 0.0)
        self.assertIsNone(speed.bridge_overhead_ratio)
        self.assertTrue(math.isfinite(speed.prefill_tps))
        self.assertTrue(math.isfinite(speed.decode_tps))

    def test_serialization_contains_metrics_but_not_prompt_or_reply(self):
        prompt_text = "private prompt must not be logged"
        reply_text = "private reply must not be logged"
        speed = compute_speed(
            prompt=prompt_text,
            reply=reply_text,
            timing=SpeedTiming(
                submitted_at=1.0,
                first_text_at=2.0,
                finished_at=4.0,
                poll_interval_s=0.5,
            ),
        )

        serialized = json.dumps(speed.to_dict(), ensure_ascii=False)
        self.assertNotIn(prompt_text, serialized)
        self.assertNotIn(reply_text, serialized)
        self.assertIn('"prompt_chars"', serialized)
        self.assertIn('"reply_tokens"', serialized)


if __name__ == "__main__":
    unittest.main()
