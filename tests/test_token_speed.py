"""Token-speed estimates: numerical behavior and privacy-safe serialization.

对齐 doc/tokenspeed.md §8：速度不可用时返回 None（不用 epsilon 硬凑）、
观测值与估算值分离、短回复/短窗口/重写有质量标记、聚合分「平均单次」与「整体吞吐」。
"""

import json
import unittest

from deepseek_web.token_speed import (
    SpeedAggregate,
    SpeedTiming,
    aggregate_speed,
    compute_speed,
)


class ComputeSpeedTests(unittest.TestCase):
    def test_rates_use_observed_windows_without_settle_credit(self):
        speed = compute_speed(
            prompt="abcd",
            reply="abcdefgh",
            timing=SpeedTiming(
                submitted_at=10.0,
                first_text_at=12.0,
                finished_at=16.0,
                last_change_at=15.0,
                poll_interval_s=1.0,
                saw_generating=True,
                streamed=True,
            ),
        )

        self.assertEqual(speed.prompt_chars, 4)
        self.assertEqual(speed.reply_chars, 8)
        self.assertEqual(speed.prompt_tokens_est, 1)
        self.assertEqual(speed.reply_tokens_est, 2)
        # 首字延迟 = 12 - 10；增长窗口 = 15 - 12；完成判定等待 = 16 - 15
        self.assertAlmostEqual(speed.page_ttft_observed_s, 2.0)
        self.assertAlmostEqual(speed.page_growth_window_s, 3.0)
        self.assertAlmostEqual(speed.settle_observed_s, 1.0)
        self.assertAlmostEqual(speed.page_generation_to_finish_s, 4.0)
        self.assertAlmostEqual(speed.request_e2e_s, 6.0)
        # 输入速率 = 1 / 2；输出速度 = 2 / 3（不再扣 settle）
        self.assertAlmostEqual(speed.prompt_rate_est_tps, 1 / 2)
        self.assertAlmostEqual(speed.page_decode_tps_est, 2 / 3)
        self.assertTrue(speed.saw_generating)
        self.assertTrue(speed.streamed)
        self.assertIn("token_estimate", speed.quality_flags)

    def test_missing_first_text_marks_no_signal_and_no_rates(self):
        speed = compute_speed(
            prompt="abcd",
            reply="",
            timing=SpeedTiming(submitted_at=5.0, first_text_at=None, finished_at=8.0),
        )

        self.assertIsNone(speed.page_ttft_observed_s)
        self.assertIsNone(speed.page_growth_window_s)
        self.assertIsNone(speed.prompt_rate_est_tps)
        self.assertIsNone(speed.page_decode_tps_est)
        self.assertIn("no_generation_signal", speed.quality_flags)

    def test_short_growth_window_yields_no_speed(self):
        # 增长窗口 0.4s 短于一个轮询周期 1.0s：速度快照不可信 -> None
        speed = compute_speed(
            prompt="abcd",
            reply="abcdefgh",
            timing=SpeedTiming(
                submitted_at=0.0,
                first_text_at=2.0,
                last_change_at=2.4,
                finished_at=3.0,
                poll_interval_s=1.0,
            ),
        )
        self.assertIsNone(speed.page_decode_tps_est)
        self.assertIn("short_growth_window", speed.quality_flags)

    def test_short_reply_is_flagged(self):
        speed = compute_speed(
            prompt="hello world",
            reply="ok",
            timing=SpeedTiming(
                submitted_at=0.0, first_text_at=2.0, last_change_at=10.0,
                finished_at=11.0, poll_interval_s=1.0,
            ),
        )
        self.assertIn("short_reply", speed.quality_flags)

    def test_rewritten_is_flagged_and_excluded_from_aggregate(self):
        speed = compute_speed(
            prompt="abcd",
            reply="abcdefgh",
            timing=SpeedTiming(
                submitted_at=0.0, first_text_at=2.0, last_change_at=10.0,
                finished_at=11.0, poll_interval_s=1.0, rewritten=True,
            ),
        )
        self.assertIn("rewritten", speed.quality_flags)
        agg = aggregate_speed(SpeedAggregate(), speed)
        self.assertEqual(agg.samples, 0)

    def test_invalid_timing_is_not_masked_as_zero(self):
        # 时间戳逆序：所有时长 None，速度 None（不用 epsilon 硬凑成极大值）
        speed = compute_speed(
            prompt="abcd",
            reply="abcdefgh",
            timing=SpeedTiming(
                submitted_at=5.0, first_text_at=4.0, last_change_at=3.0,
                finished_at=2.0, poll_interval_s=1.0,
            ),
        )
        self.assertIsNone(speed.page_ttft_observed_s)
        self.assertIsNone(speed.page_decode_tps_est)
        self.assertIn("invalid_timing", speed.quality_flags)

    def test_serialization_contains_metrics_but_not_prompt_or_reply(self):
        prompt_text = "private prompt must not be logged"
        reply_text = "private reply must not be logged"
        speed = compute_speed(
            prompt=prompt_text,
            reply=reply_text,
            timing=SpeedTiming(
                submitted_at=1.0, first_text_at=2.0, last_change_at=4.0,
                finished_at=5.0, poll_interval_s=0.5,
            ),
        )

        serialized = json.dumps(speed.to_dict(), ensure_ascii=False)
        self.assertNotIn(prompt_text, serialized)
        self.assertNotIn(reply_text, serialized)
        self.assertIn('"prompt_chars"', serialized)
        self.assertIn('"reply_tokens_est"', serialized)
        self.assertIn('"quality_flags"', serialized)


class SpeedAggregateTests(unittest.TestCase):
    @staticmethod
    def _speed(ttft: float, growth: float, reply: str = "abcdefgh") -> object:
        return compute_speed(
            prompt="abcd",
            reply=reply,
            timing=SpeedTiming(
                submitted_at=0.0,
                first_text_at=ttft,
                last_change_at=ttft + growth,
                finished_at=ttft + growth + 0.5,
                poll_interval_s=0.0,
            ),
        )

    def test_empty_aggregate_rates_are_none(self):
        agg = SpeedAggregate()
        self.assertEqual(agg.samples, 0)
        self.assertIsNone(agg.mean_decode_tps_est)
        self.assertIsNone(agg.aggregate_decode_tps_est)
        self.assertIsNone(agg.ttft_s_avg)

    def test_mean_vs_aggregate_differ_with_weighting(self):
        # 样本1：2 tok / 1s；样本2：2 tok / 9s
        agg = SpeedAggregate()
        agg = aggregate_speed(agg, self._speed(2.0, 1.0))   # 2 tok / 1s
        agg = aggregate_speed(agg, self._speed(2.0, 9.0))   # 2 tok / 9s
        self.assertEqual(agg.samples, 2)
        # 平均单次：两轮速率之和除以样本数，即 (2/1 + 2/9) / 2
        self.assertAlmostEqual(agg.mean_decode_tps_est, (2 / 1 + 2 / 9) / 2)
        # 整体吞吐：总 token 数除以总时长，即 (2+2) / (1+9)
        self.assertAlmostEqual(agg.aggregate_decode_tps_est, 4 / 10)

    def test_aggregate_does_not_mutate_receiver(self):
        agg = SpeedAggregate()
        _ = aggregate_speed(agg, self._speed(2.0, 4.0))
        self.assertEqual(agg.samples, 0)


if __name__ == "__main__":
    unittest.main()
